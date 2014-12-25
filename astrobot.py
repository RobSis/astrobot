#!/usr/bin/env python

import client   # Astrometry.net API
import praw     # Reddit API
import pyimgur  # Imgur API

import math
import time
import io
import sys
import subprocess

import argparse
import urlparse
from string import Template
from collections import deque
import logging

import urllib2
import requests
from lxml import etree
from PIL import Image

import credentials


NEW_POSTS = 100     # number of new posts to go through
MAX_SKIPPED = 1000  # remember only last n skipped posts
MAX_TAGS = 8        # when there's more than n tags, filter out the stars
REST_TIME = 180     # time to rest between each loop
ERROR_TIME = 60     # time to rest on API error
TTL = 10            # number of tries for every post


class AstroBot:
    def __init__(self):
        # Imgur API
        self.imgur = pyimgur.Imgur(credentials.IMGUR_CLIENT_ID, \
                                client_secret=credentials.IMGUR_CLIENT_SECRET)
        authorized = False
        # TODO: automatize this
        while (not authorized):
            sys.stdout.flush()
            print "Get PIN on", credentials.IMGUR_AUTH_URL
            pin = raw_input("Enter PIN: ")
            try:
                self.imgur.exchange_pin(pin)
                authorized = True
            except:
                print "[ERROR]:", "Wrong PIN or other error. Authorize again."

        # Astrometry API
        self.astrometry = client.client.Client()
        self.astrometry.login(credentials.ASTROMETRY_ID)

        # Reddit API
        self.praw = praw.Reddit(user_agent=credentials.USER_AGENT)
        self.praw.login(credentials.REDDIT_USER, credentials.REDDIT_PASSWORD)

        # set of submissions currently being solved
        # TODO: consider pickle
        self.solving = dict()

        # queue of skipped reddit ids
        self.skipped = deque(maxlen=MAX_SKIPPED)

        # blacklist of words on /r/astrophotography+apod
        self.blacklist = ["moon", "lunar", "sun", "solar", "eclipse",\
            "mercury", "venus", "mars", "jupiter", "saturn", "uranus",\
            "neptune", "trails", "panorama"]

        # whitelist of words on /r/astronomy+space+spaceporn
        self.whitelist = ["galaxy", "ngc", "comet", "nebula", "constellation",\
                "iss", "ison", "sky", "skies"]

        # logging the solved posts
        self.logger = logging.getLogger("astrobot")
        hdlr = logging.FileHandler("solved.log")
        formatter = logging.Formatter('%(message)s')
        hdlr.setFormatter(formatter)
        self.logger.addHandler(hdlr)
        self.logger.setLevel(logging.INFO)

    def run(self):
        """
        The main loop.
        """
        while True:
            try:
                self.refresh()

                self.read_inbox()

                self.process_new()

                self.check_for_solved()

                print "[INFO]:", "Sleeping for %d minute(s)." % (REST_TIME / 60)
                time.sleep(REST_TIME)
            except (praw.errors.APIException, requests.exceptions.HTTPError, urllib2.HTTPError) as e:
                print "[WARN]:", "API error. Sleeping for %d minute(s)." % (ERROR_TIME / 60)
                print "[WARN]:", e
                time.sleep(ERROR_TIME)
            except (KeyboardInterrupt, EOFError), e:
                print "\n(quit)"
                return -1
            except:
                print "[WARN]:", "Sleeping for %d minute(s)." % (ERROR_TIME / 60)
                print "[WARN]:", e
                time.sleep(ERROR_TIME)

    def refresh(self):
        """
        Refresh the imgur access token.
        """
        self.imgur.refresh_access_token()

    def process_new(self):
        """
        Find posts to process and send them to nova.Astrometry.net
        """
        subreddits = self.praw.get_subreddit("astrophotography+astronomy+space+spaceporn+apod")

        # get last 100 posts
        for post in subreddits.get_new(limit=NEW_POSTS):
            if self._check_condition(post):
                self._send_for_solution(post)
            else:
                self.skipped.append(post.id)

        # get hidden posts
        for post in self.praw.user.get_hidden():
            post.unhide()
            if self._check_condition(post, force=True):
                self._send_for_solution(post)
            else:
                self.skipped.append(post.id)

    def check_for_solved(self):
        """
        Poll for the status of every solution
        and process successful ones.
        """
        for subid in list(self.solving):
            metadata = self.solving[subid]
            result = self.astrometry.send_request("submissions/%d" % subid)
            if len(result["job_calibrations"]) != 0:
                metadata["job_id"] = result["job_calibrations"][0][0]
                metadata["image_id"] = result["user_images"][0]

                self._post_solved(metadata)

                self.skipped.append(metadata["post"].id)
                del(self.solving[subid])
                continue

            metadata["TTL"] -= 1
            if metadata["TTL"] < 1:
                print "[WARN]:", "Failed to solve the post in time."
                self.skipped.append(metadata["post"].id)
                del(self.solving[subid])

    def read_inbox(self):
        """
        Read the inbox for deletion messages.
        """
        message_ok = Template("The automatic comment for "
                              "[your submission]($permalink) "
                              "was removed.\n\nIf you want to disable the bot "
                              "for all your future submissions, send me a PM! "
                              "\n\nSorry for inconvenience.")

        message_not_ok = ("It seems you don't have right to remove the comment."
                          "If you believe you do, send me a PM!")

        for msg in self.praw.get_inbox():
            if "delete" in msg.subject and msg.new:
                print "[INFO]:", "Deletion request was received."
                remove_id = msg.body
                deleted = False
                me = self.praw.get_redditor(credentials.REDDIT_USER)
                for c in me.get_comments(limit=None):
                    if c.id == remove_id and msg.author == c.submission.author:
                        c.delete()
                        msg.mark_as_read()
                        deleted = True
                        break

                if deleted:
                    print "[INFO]:", "Deletion successful."
                    self.praw.send_message(msg.author, 'Comment removed',
                            message_ok.safe_substitute({"permalink": c.submission.permalink}))
                else:
                    self.praw.send_message(msg.author, 'Error while processing request',
                            message_not_ok)
                    msg.mark_as_read()

    # --- helper methods
    def _check_condition(self, post, force=False):
        """
        Decide whether to process the reddit post.
        """
        if not force and post.id in self.skipped:
            return False

        if post.saved:
            return False

        blacklist_matches = sum(word in post.title.lower() for word in self.blacklist)
        whitelist_matches = sum(word in post.title.lower() for word in self.whitelist)
        if post.subreddit.display_name.lower() in ["astrophotography", "apod"]:
            if not force and blacklist_matches == 1 and whitelist_matches == 0:
                return False
        else:
            if not force and whitelist_matches == 0:
                return False

        # TODO: fix 'load more comments'
        post.replace_more_comments(limit=16, threshold=1)
        for comment in praw.helpers.flatten_tree(post.comments):
            if isinstance(comment, praw.objects.Comment) and \
                    "astrometry.net" in comment.body.lower():
                return False

        try:
            if (self._parse_url(post.url) is None):
                return False
        except urllib2.HTTPError as e:
            print "[INFO]:", "Location can't be opened."
            return False

        return True

    def _send_for_solution(self, post):
        """
        Process the reddit post and send
        to nova.Astrometry.net.
        """
        image_url = self._parse_url(post.url)

        # get resolution of photo (used for computing range)
        fd = urllib2.urlopen(image_url)
        image_file = io.BytesIO(fd.read())
        im = Image.open(image_file)

        # upload
        print "[INFO]:", "Sending post", post.permalink, "to nova.Astrometry.net"
        subid = self._upload(image_url)

        metadata = dict()
        metadata["id"] = subid
        metadata["post"] = post
        metadata["image_size"] = im.size

        metadata["TTL"] = TTL

        if subid not in self.solving:
            self.solving[subid] = metadata
            self.skipped.append(post.id)

    def _post_solved(self, metadata):
        """
        Post results of solved submission to the
        comment section of the reddit post.
        """
        post = metadata["post"]

        # calibration
        (ra, de, radius, rg) = self._get_calibration(metadata["job_id"], metadata["image_size"])
        metadata["rectascension"] = ra
        metadata["declination"] = de
        metadata["range"] = rg
        metadata["radius"] = radius

        # annotated image
        metadata["author"] = ""
        if ("astrophotography" in post.subreddit.display_name.lower()):
            metadata["author"] = post.author.name
        metadata["annotated_image"] = self._upload_annotated(metadata["job_id"], metadata["author"])

        # tags
        metadata["tags"] = self._get_tags(metadata["job_id"])
        if (metadata["annotated_image"] is not None):
            comment = self._generate_comment(metadata)
            c = post.add_comment(comment)

            time.sleep(4)
            c.edit(comment.replace('____id____', str(c.id)))
            post.upvote()  # can I do that?
            post.save()

            self.logger.info("%s:%s" % (str(metadata["id"]), post.id))
            print "[INFO]:", "Post", post.permalink, "successfully solved."

    def _parse_url(self, rawUrl):
        """
        Get direct image URL for web pages.
        """
        url = urlparse.urlparse(rawUrl)

        # check whether the url is accessible
        req = urllib2.Request(rawUrl, headers={'User-Agent' : credentials.USER_AGENT})
        fd = urllib2.urlopen(req)

        p = url.path.lower()
        if p.endswith(".jpg") or p.endswith(".jpeg") or p.endswith(".png") or p.endswith(".gif"):
            return rawUrl

        # get direct url from imgur (skip sets and albums)
        if "imgur.com" in url.netloc and "a/" not in url.path and ("," not in url.path) and ("gifv" not in url.path):
            newloc = "i." + url.netloc
            newpath = url.path
            if newpath.endswith("/new"):
                newpath.replace("/new", "")
            newpath += ".jpg"
            newpath = newpath.replace("gallery/", "")
            newUrl = urlparse.ParseResult(url.scheme, newloc, newpath,
                        url.params, url.query, url.fragment)

            return newUrl.geturl()

        # get direct url from flickr
        if "flickr.com" in url.netloc:
            path = filter(lambda x: x != '', url.path.split('/'))
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

        if "wikipedia.org" in url.netloc and "File:" in url.path:
            try:
                file = urllib2.urlopen(url.geturl())
                tree = etree.HTML(file.read())
                directUrl = tree.xpath('//div[@class="fullMedia"]/a/@href')[0]
                if len(directUrl):
                    return "http:" + directUrl
            except:
                pass

        return None

    def _upload(self, image_url):
        """
        Upload the image to Astrometry and return submission id.
        """
        kwargs = dict(
                allow_commercial_use="n",
                allow_modifications="n",
                publicly_visible="y")
                #TODO: downsample if image too big...

        result = self.astrometry.url_upload(image_url, **kwargs)

        stat = result['status']
        if stat != 'success':
            print "[WARN]:", "Upload failed: status", stat
            return

        return result['subid']

    # TODO: rewrite to pure python solution
    def _upload_annotated(self, job_id, author):
        """
        Get annotated image from astrometry, put label on it and upload to Imgur.
        """

        subprocess.check_call(["./annotate.sh", str(job_id), author])

        self.imgur.refresh_access_token()
        try:
            uploaded_image = self.imgur.upload_image(path=str(job_id) + ".png",\
                                album=credentials.ALBUM_ID)
            return uploaded_image.link
        except:
            print "[WARN]:", "Imgur error. Image not uploaded."
            return None

    def _get_tags(self, job_id):
        """
        Get the resolved objects.
        """
        tags = self.astrometry.send_request('jobs/%s/tags' % job_id, {})["tags"]
        # if there are too many tags, filter out stars
        if (len(tags) > MAX_TAGS):
            tags = filter(lambda x: x.find("star") == -1, tags)

        return tags

    def _get_calibration(self, job_id, image_size):
        """
        Get calibration of solved job and parse it to
        r. ascension, declination, radius and range.
        """
        calibration = self.astrometry.send_request('jobs/%s/calibration' % str(job_id))
        ra = calibration['ra']
        de = calibration['dec']
        radius = calibration['radius']

        max_span = max(image_size)
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

        return (float(ra), float(de), float(radius), float(rg))

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

    def _wikisky_link(self, metadata):
        link = "http://server4.wikisky.org/v2"

        link += "?ra=" + str(metadata["rectascension"] / 15.0)
        link += "&de=" + str(metadata["declination"])

        zoom = 18 - round(math.log(metadata["range"] / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        link += "&show_grid=1&show_constellation_lines=1"
        link += "&show_constellation_boundaries=1&show_const_names=1"
        link += "&show_galaxies=1&img_source=SKY-MAP"

        return link

    def _googlesky_link(self, metadata):
        link = "http://www.google.com/sky/"

        link += "#longitude=" + str(metadata["rectascension"] - 180)
        link += "&latitude=" + str(metadata["declination"])

        zoom = 20 - round(math.log(metadata["range"] / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        return link

    def _generate_comment(self, metadata):
        """
        Construct the comment for reddit.
        """
        comment = ("*This is an automatically generated comment.*\n\n"
                   "---\n\n"
                   "$coordinates"
                   "$radius"
                   "$image"
                   "$tags"
                   "$links"
                   "---\n\n"
                   "$advertise"
                   "^Powered ^by [^Astrometry.net]("
                   "http://nova.astrometry.net/user_images/$image_id)"
                   " ^| [^Feedback]("
                   "http://www.reddit.com/message/compose?to=astro-bot)"
                   " ^| [^FAQ]("
                   "http://www.reddit.com/r/faqs/comments/1ninoq/uastrobot_faq/)"
                   " ^| ^1) ^Tags ^may ^overlap"
                   " ^| ^OP ^can [^delete]("
                   "http://www.reddit.com/message/compose?to=astro-bot&subject=delete&message=____id____) ^this ^comment."
                   )

        # data model for the template
        model = dict()
        model["advertise"] = ""
        if (metadata["post"].subreddit.display_name.lower() != "astrophotography"):
            model["advertise"] = "*If this is your photo, consider x-posting to /r/astrophotography!*\n\n"

        model["coordinates"] = "> Coordinates: "
        model["coordinates"] += "%d^h %d^m %.2f^s , " % self._real_to_hours(metadata["rectascension"] / 15.0)
        model["coordinates"] += "%d^o %d' %.2f\"\n\n" % self._real_to_hours(metadata["declination"])

        model["radius"] = "> Radius: %.3f deg\n\n" % metadata["radius"]

        model["image"] = "> Annotated image: [%s](%s)\n\n" % (metadata["annotated_image"], metadata["annotated_image"])

        if (len(metadata["tags"]) > 0):
            model["tags"] = "> Tags^1: *" + ", ".join(metadata["tags"]) + "*\n\n"
        else:
            model["tags"] = ""

        model["links"] = "> Links: "
        model["links"] += "[Google Sky](%s) | " % self._googlesky_link(metadata)
        model["links"] += "[WIKISKY.ORG](%s)\n\n" % self._wikisky_link(metadata)

        model["image_id"] = metadata["image_id"]

        return Template(comment).safe_substitute(model)


if __name__ == '__main__':
    bot = AstroBot()
    sys.exit(bot.run())
