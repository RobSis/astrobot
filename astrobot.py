#!/usr/bin/env python

import client
import praw
import pyimgur

import sys
import math
import time
import argparse
import urlparse
import subprocess
import io
from string import Template
import logging

import urllib2
import requests
from lxml import etree
from PIL import Image

import credentials

class AstroBot(object):
    def __init__(self, pin=None):
        # Imgur API
        self.imgur = pyimgur.Imgur(credentials.IMGUR_CLIENT_ID, client_secret=credentials.IMGUR_CLIENT_SECRET)
        authorized = False

        if (pin is not None):
            try:
                self.imgur.exchange_pin(pin)
                authorized = True
            except:
                print "[ERROR]", "Wrong PIN or other error. Authorize again."
                sys.exit(-1)
        else:
            while (not authorized):
                sys.stdout.flush()
                print "Get PIN on", credentials.IMGUR_AUTH_URL
                pin = raw_input("Enter PIN: ")
                try:
                    self.imgur.exchange_pin(pin)
                    authorized = True
                except:
                    print "Wrong PIN or other error. Authorize again."

        # Astrometry API
        self.api = client.client.Client()
        self.api.login(credentials.ASTROMETRY_ID)

        # Reddit API
        self.praw = praw.Reddit(user_agent = credentials.USER_AGENT)
        self.praw.login(credentials.REDDIT_USER, credentials.REDDIT_PASSWORD)

        # set of skipped submissions
        self.skipped = []

        # blacklist of words on /r/astrophotography+astropix
        self.blacklist = ["moon", "lunar", "sun", "solar", "eclipse",\
            "mercury", "venus", "mars", "jupiter", "saturn", "uranus", \
            "neptune", "trails", "panorama"]

        # whitelist of words on /r/astronomy+space+spaceporn
        self.whitelist = ["galaxy", "ngc", "comet", "nebula", "constellation", "iss",
                     "ison", "sky", "skies"]

        # logging
        self.logger = logging.getLogger("astrobot")
        hdlr = logging.FileHandler("solved.log")
        formatter = logging.Formatter('%(message)s')
        hdlr.setFormatter(formatter)
        self.logger.addHandler(hdlr)
        self.logger.setLevel(logging.INFO)

    def _process(self, thread, submission_id = None):
        """Process the reddit submission"""
        self.info = dict()
        self.info["rectascension"] = 0
        self.info["declination"] = 0
        self.info["range"] = 0
        self.info["tags"] = []
        self.info["annotated_image"] = ""
        self.info["job_id"] = 0
        self.info["image_id"] = 0
        self.info["author"] = ""

        imageURL = self._parse_url(thread.url)
        if (imageURL is None):
            print "[WARN]:", "Submission link doesn's seem to be an image"
            self.skipped.append(thread.id)
            return

        # get resolution of photo (used for computing range)
        fd = urllib2.urlopen(imageURL)
        image_file = io.BytesIO(fd.read())
        im = Image.open(image_file)
        self.info["image_size"] = im.size

        if (not submission_id):
            submission = self._upload(imageURL)
        else:
            submission = self.api.sub_status(submission_id, justdict=True)
            submission['subid'] = submission_id

        self.info["job_id"] = submission['jobs'][0]
        self.info["image_id"] = submission['user_images'][0]

        success = self._wait_for_job(self.info["job_id"])
        if (not success):
            print "[WARN]:", "Failed to solve the picture."
            thread.save()
            return

        self.info["author"] = str(thread.author)
        if ("astrophotography" not in thread.subreddit.display_name.lower()):
            self.info["author"] = ""
        if ("apod." in thread.url.lower()):
            self.info["author"] = ""

        self.info["tags"] = self.api.send_request('jobs/%s/tags' % self.info["job_id"], {})["tags"]
        # if there are too many tags, filter out stars
        if (len(self.info["tags"]) > 8):
            self.info["tags"] = filter(lambda x: x.find("star") == -1, self.info["tags"])

        (ra, de, radius, rg) = self._get_calibration(self.info)
        self.info["rectascension"] = ra
        self.info["declination"] = de
        self.info["range"] = rg
        self.info["radius"] = radius
        self.info["annotated_image"] = self._upload_annotated(self.info)

        if (self.info["annotated_image"] is not None):
            # Post to reddit
            advertise = False
            if (thread.subreddit.display_name.lower() != "astrophotography"):
                advertise = True

            comment = self._generate_comment(self.info, advertise)
            c = thread.add_comment(comment)
            time.sleep(2)
            c.edit(comment.replace('____id____', str(c.id)))
            thread.upvote() # can I do that?
            thread.save()

            self.logger.info("%s:%s" % (str(submission['subid']), thread.id))


    def _check_condition(self, submission):
        "Decide whether to process the submission"
        if submission.id in self.skipped:
            return False

        if submission.saved:
            return False

        blacklist_matches = sum(word in submission.title.lower() for word in self.blacklist)
        whitelist_matches = sum(word in submission.title.lower() for word in self.whitelist)
        if submission.subreddit.display_name.lower() in ["astrophotography", "astropix"]:
            if blacklist_matches == 1 and whitelist_matches == 0:
                return False
        else:
            if whitelist_matches == 0:
                return False

        submission.replace_more_comments(limit=16, threshold=1)
        for comment in praw.helpers.flatten_tree(submission.comments):
            if isinstance(comment, praw.objects.Comment) and \
                    "astrometry.net" in comment.body.lower():
                return False

        return True

    def _parse_url(self, rawUrl):
        url = urlparse.urlparse(rawUrl)

        if url.netloc == "i.imgur.com":
            return rawUrl

        # get direct url from imgur
        if "imgur.com" in url.netloc and ("a/" not in url.path):
            newloc = "i." + url.netloc
            newpath = url.path + ".jpg"
            newpath = newpath.replace("gallery/","")
            newUrl = urlparse.ParseResult(url.scheme, newloc, newpath,
                        url.params, url.query, url.fragment)

            return newUrl.geturl()

        # get direct url from flickr
        if "flickr.com" in url.netloc:
            path = filter(lambda x : x != '', url.path.split('/'))
            if path[0] == 'photos' and len(path) >= 3:
                newpath = '/photos/%s/%s/sizes/l' % (path[1], path[2])
                newUrl = urlparse.ParseResult(url.scheme, url.netloc, newpath,
                        url.params, url.query, url.fragment)

                try:
                    file = urllib2.urlopen(newUrl.geturl())
                    tree = etree.HTML(file.read())
                    staticUrl = tree.xpath('//div[@id="allsizes-photo"]/img/@src')
                    if len(staticUrl):
                        return staticUrl[0]
                except:
                    pass

        if "apod.nasa.gov" in url.netloc:
            try:
                file = urllib2.urlopen(url.geturl())
                tree = etree.HTML(file.read())
                directUrl = tree.xpath('//img/@src')
                if len(directUrl):
                    return "http://apod.nasa.gov/apod/" + directUrl[0]
            except:
                pass

        p = url.path.lower()
        if p.endswith(".jpg") or p.endswith(".jpeg") or p.endswith(".png"):
            return rawUrl

        return None

    def _upload(self, image_url):
        """Uploads the image on given url to Astrometry and waits for job id."""

        kwargs = dict(
                allow_commercial_use="n",
                allow_modifications="n",
                publicly_visible="y")

        result = self.api.url_upload(image_url, **kwargs)

        stat = result['status']
        if stat != 'success':
            print "[WARN]:", "Upload failed: status", stat
            return

        sub_id = result['subid']
        job_id = None
        image_id = None
        tries = 0
        while tries < 40:
            subStat = self.api.sub_status(sub_id, justdict=True)
            jobs = subStat.get('jobs',[])
            if len(jobs):
                for j in jobs:
                    if j is not None:
                        break
                if j is not None:
                    break
            print "sleeping 5s"
            time.sleep(5)
            tries += 1
        subStat['subid'] = sub_id
        return subStat

    # TODO: rewrite to python
    def _upload_annotated(self, info):
        """Get annotated image from astrometry, put label on it and upload to imgur"""

        subprocess.check_call(["./annotate.sh", str(info["job_id"]), info["author"]])

        self.imgur.refresh_access_token()
        try:
            uploaded_image = self.imgur.upload_image(path=str(info["job_id"]) + ".png", album=credentials.ALBUM_ID)
            return uploaded_image.link
        except:
            print "[WARN]:", "Imgur error. Image not uploaded."
            return None

    def _wait_for_job(self, job_id):
        """Wait for the result of job."""

        tries = 0
        while tries < 40:  # don't spend too much time on solving
            stat = self.api.job_status(job_id, justdict=True)
            if stat and stat.get('status','') in ['success']:
                return True
            if stat and stat.get('status','') in ['failure']:
                return False
            print "sleeping 5s"
            time.sleep(5)
            tries += 1
        return False

    def _get_calibration(self, info):
        """Get calibration of solved job and parse it."""

        calibration = self.api.send_request('jobs/%s/calibration' % info['job_id'])
        ra = calibration['ra']
        de = calibration['dec']
        radius = calibration['radius']

        max_span = max(info['image_size'])
        angular_scale = calibration['pixscale'] * max_span / 3600.0

        TINY_FLOAT_VALUE = 1.0e-8
        RADIUS_EARTH = 6378135.0        # in meters
        VIEWABLE_ANGULAR_SCALE = 50.0   # in degrees

        alpha = 0.5 * VIEWABLE_ANGULAR_SCALE * (math.pi / 180.0)
        beta = 0.5 * angular_scale * (math.pi / 180.0)
        if (beta > alpha):
            beta = alpha
        rg = RADIUS_EARTH * (1.0 - (math.sin(alpha - beta) /\
                                   (math.sin(alpha) + TINY_FLOAT_VALUE)))

        return (ra, de, radius, rg)

    def _hours_to_real(self, hours, minutes, seconds):
        return hours + minutes / 60.0 + seconds / 3600.0

    def _real_to_hours(self, real):
        hours = int(real)
        n = real - hours
        if (n < 0):
            n = -n

        minutes = int(math.floor(n * 60))
        n = n - minutes / 60.0
        seconds = n * 3600

        return (hours, minutes, seconds)

    def _wikisky_link(self, info):
        link = "http://server4.wikisky.org/v2"

        link += "?ra=" + str(info["rectascension"] / 15.0)
        link += "&de=" + str(info["declination"])

        zoom = 18 - round(math.log(info["range"] / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        link += "&show_grid=1&show_constellation_lines=1"
        link += "&show_constellation_boundaries=1&show_const_names=1"
        link += "&show_galaxies=1&img_source=SKY-MAP"

        return link

    def _googlesky_link(self, info):
        link = "http://www.google.com/sky/"

        link += "#longitude=" + str(info["rectascension"] - 180)
        link += "&latitude=" + str(info["declination"])

        zoom = 20 - round(math.log(info["range"] / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        return link

    def _generate_comment(self, info, advertise):
        """Construct the comment for reddit."""

        data = dict()

        data["coordinates"] = "> Coordinates"

        (hh, mm, ss) = self._real_to_hours(info["rectascension"] / 15.0)
        data["hh"] = '%d^h' % hh
        data["mm"] = '%d^m' % mm
        data["ss"] = '%.2f^s' % ss

        (hh, mm, ss) = self._real_to_hours(info["declination"])
        data["h2"] = '%d^o' % hh
        data["m2"] = '%d\'' % mm
        data["s2"] = '%.2f"' % ss

        data["radius"] = "> Radius: %.3f deg\n\n" % info["radius"]

        imageLinks = "> Annotated image: [$annotated_image]($annotated_image)\n\n"
        data["image"] = Template(imageLinks).safe_substitute(info)

        if (len(info["tags"]) > 0):
            data["tags"] = "> Tags^1: *" + ", ".join(info["tags"]) + "*\n\n"
        else:
            data["tags"] = ""

        data["google"] = "[Google Sky](" + self._googlesky_link(info) + ")"
        data["wikisky"] = "[WIKISKY.ORG](" + self._wikisky_link(info) + ")"
        data["links"] = Template("> Links: $google | $wikisky\n\n").safe_substitute(data)

        data["image_id"] = info["image_id"]

        message =  "*This is an automatically generated comment.*\n\n"
        message += "---\n\n"
        message += "$coordinates: $hh $mm $ss , $h2 $m2 $s2\n\n"
        message += "$radius"
        message += "$image"
        message += "$tags"
        message += "$links"
        message += "---\n\n"
        if (advertise):
            message += "*If this is your photo, consider x-posting to /r/astrophotography!*\n\n"
        message += "^Powered ^by [^Astrometry.net]("
        message += "http://nova.astrometry.net/user_images/$image_id)"
        message += " ^| [^Feedback]("
        message += "http://www.reddit.com/message/compose?to=astro-bot)"
        message += " ^| [^FAQ]("
        message += "http://www.reddit.com/r/faqs/comments/1ninoq/uastrobot_faq/)"
        message += " ^| ^1) ^Tags ^may ^overlap"
        message += " ^| ^OP ^can [^delete]("
        message += "http://www.reddit.com/message/compose?to=astro-bot&subject=delete&message=____id____) ^this ^comment."

        return Template(message).safe_substitute(data)

    def run(self):
        running = True
        while running:
            try:
                # react on deletion messages
                for msg in self.praw.get_inbox():
                    if "delete" in msg.subject and msg.new:
                        print "[INFO]:", "Deletion request was received."
                        remove_id = msg.body
                        deleted = False
                        me = self.praw.get_redditor("astro-bot")
                        for c in me.get_comments(limit=None):
                            if c.id == remove_id and msg.author == c.submission.author:
                                c.delete()
                                msg.mark_as_read()
                                deleted = True
                                break

                        if deleted:
                            print "[INFO]:", "Deletion successful."
                            self.praw.send_message(msg.author, 'Comment removed',
                                "The automatic comment for [your submission]"
                                + "(" + str(c.submission.permalink) + ") " +
                                "was removed.\n\nIf you want to disable the bot "
                                "for all your future submissions, send me a PM!"
                                "\n\nSorry for inconvenience.")
                        else:
                            self.praw.send_message(msg.author, 'Error while processing request',
                                "It seems you don't have right to remove the comment. "
                                "If you believe you do, send me a PM.")
                            msg.mark_as_read()

                # search subreddits
                subreddits = self.praw.get_subreddit("astrophotography+astronomy+space+spaceporn+apod")
                for submission in subreddits.get_new(limit = 100):
                    print "[INFO]:", "Processing submission", submission.permalink
                    if self._check_condition(submission):
                        self._process(submission)
                    else:
                        print "[WARN]:", "Decided not to process the submission"
                        self.skipped.append(submission.id)
                    print

                for submission in self.praw.user.get_hidden():
                    submission.unhide()
                    print "[INFO]:", "Processing submission", submission.permalink
                    self._process(submission)
                    print

                print "[INFO]:", "sleeping 3 minutes"
                time.sleep(180)
            except (praw.errors.APIException, requests.exceptions.HTTPError):
                print "[INFO]:", "sleeping 30 sec"
                time.sleep(30)

    def post_solved(self, submission, thread):
        """Post results of solved job to given thread."""
        thread = self.praw.get_submission(url=thread)
        self._process(thread, submission)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='astrobot')
    parser.add_argument(
            "-p", "--pin",
            help="Imgur PIN")
    group = parser.add_argument_group('manually')
    group.add_argument(
            "-s", "--submission",
            help="ID of submission")
    group.add_argument(
            "-t", "--thread",
            help="Reddit thread URL")
    args = parser.parse_args()

    try:
        bot = AstroBot(pin=args.pin)
        if (not args.submission or not args.thread):
            bot.run()
        else:
            bot.post_solved(args.submission, args.thread)
    except (KeyboardInterrupt, EOFError), e:
        print "\n(quit)"
        sys.exit(-1)
