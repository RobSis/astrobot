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
from string import Template

import urllib2
import zipfile
import tempfile
from xml.dom.minidom import parseString
from lxml import etree
import requests

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

        self._rightAscension = 0
        self._declination = 0
        self._range = 0
        self._tags = []
        self._annotated_image = ""

        # set of skipped submissions
        self.skipped = []

        # blacklist of words on /r/astrophotography+apod
        self.blacklist = ["moon", "lunar", "sun", "solar", "eclipse", "mercury", "venus", "mars", "jupiter", "uranus", "neptune"]

        # whitelist of words on /r/astronomy+space+spaceporn
        self.whitelist = ["galaxy", "ngc", "comet", "nebula", "constellation", "iss",
                     "ison", "sky", "skies"]

    def _process(self, thread):
        self.image = self._parse_url(thread.url)
        if (self.image is None):
            print "[WARN]:", "Submission link doesn's seem to be an image"
            self.skipped.append(thread.id)
            return

        job_id = self._upload(self.image)
        success = self._wait_for_job(job_id)
        if (success):
            self._process_solved(thread, job_id)
        else:
            print "[WARN]:", "Failed to solve the picture."
            thread.save()

    def _process_solved(self, thread, job_id):
        self.author = str(thread.author)
        if ("astrophotography" not in thread.subreddit.display_name.lower()):
            self.author = ""
        if ("apod." in thread.url.lower()):
            self.author = ""
        self.job_id = job_id

        self._tags = self.api.send_request('jobs/%s/tags' % self.job_id, {})["tags"]
        # if there are too many tags, filter out stars
        if (len(self._tags) > 8):
            self._tags = filter(lambda x: x.find("star") == -1, self._tags)

        self._parse_kml("http://nova.astrometry.net/kml_file/" + self.job_id)
        self._annotated_image = self._upload_annotated()

        if (self._annotated_image is not None):
            # Post to reddit
            thread.add_comment(self._create_comment())
            thread.upvote() # can I do that?
            thread.save()

            # Save the comment


    def _check_condition(self, submission):
        "Decide whether to process the submission"
        if submission.id in self.skipped:
            return False

        if submission.saved:
            return False

        blacklist_matches = sum(word in submission.title.lower() for word in self.blacklist)
        whitelist_matches = sum(word in submission.title.lower() for word in self.whitelist)
        if submission.subreddit.display_name.lower() == "astrophotography" or\
                submission.subreddit.display_name.lower() == "apod":
            if blacklist_matches == 1 and whitelist_matches == 0:
                return False
        else:
            if whitelist_matches == 0:
                return False

        for comment in praw.helpers.flatten_tree(submission.comments):
            if "astrometry.net" in comment.body.lower():
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
        tries = 0
        while tries < 40:
            subStat = self.api.sub_status(sub_id, justdict=True)
            jobs = subStat.get('jobs',[])
            if len(jobs):
                for j in jobs:
                    if j is not None:
                        break
                if j is not None:
                    job_id = j
                    break
            print "sleeping 5s"
            time.sleep(5)
            tries += 1
        return str(job_id)

    # TODO: rewrite to python
    def _upload_annotated(self):
        """Get annotated image from astrometry, put label on it and upload to imgur"""

        subprocess.check_call(["./annotate.sh", self.job_id, self.author])

        self.imgur.refresh_access_token()
        try:
            uploaded_image = self.imgur.upload_image(path=self.job_id + ".png", album=credentials.ALBUM_ID)
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

    def _parse_kml(self, path):
        """Download KML file of solved job and parse it."""

        file = urllib2.urlopen(path)
        pkdata = file.read()
        tmp = tempfile.NamedTemporaryFile()
        tmp.write(pkdata)
        tmp.flush()

        zf = zipfile.ZipFile(tmp.name)
        data = zf.read("doc.kml")

        zf.close()
        file.close()

        #parse the xml you got from the file
        dom = parseString(data)

        longitude = dom.getElementsByTagName('longitude')[0].firstChild.nodeValue
        self._rightAscension = (float(longitude) + 180)/15.0
        self._declination = float(dom.getElementsByTagName('latitude')[0].firstChild.nodeValue)
        self._range = float(dom.getElementsByTagName('range')[0].firstChild.nodeValue)

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

    def _wikisky_link(self):
        link = "http://server4.wikisky.org/v2"

        link += "?ra=" + str(self._rightAscension)
        link += "&de=" + str(self._declination)

        zoom = 18 - round(math.log(self._range / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        link += "&show_grid=1&show_constellation_lines=1"
        link += "&show_constellation_boundaries=1&show_const_names=1"
        link += "&show_galaxies=1&img_source=SKY-MAP"

        return link

    def _googlesky_link(self):
        link = "http://www.google.com/sky/"

        link += "#latitude=" + str(self._declination)
        link += "&longitude=" + str(self._rightAscension*15 - 180)

        zoom = 20 - round(math.log(self._range / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        return link

    def _create_comment(self):
        """Construct the comment for reddit."""

        data = dict()
        data["coordinates"] = "> [Coordinates](http://en.wikipedia.org/wiki/Celestial_coordinate_system)"

        (hh, mm, ss) = self._real_to_hours(self._rightAscension)
        data["hh"] = '%d^h' % hh
        data["mm"] = '%d^m' % mm
        data["ss"] = '%.2f^s' % ss

        (hh, mm, ss) = self._real_to_hours(self._declination)
        data["h2"] = '%d^o' % hh
        data["m2"] = '%d\'' % mm
        data["s2"] = '%.2f"' % ss

        if (self._annotated_image is not None):
            imageLinks = "> Annotated image: [$url]($url)\n\n"
            data["image"] = Template(imageLinks).safe_substitute(
                    {"url":self._annotated_image})

        if (len(self._tags) > 0):
            data["tags"] = "> Tags^1: *" + ", ".join(self._tags) + "*\n\n"
        else:
            data["tags"] = ""

        data["google"] = "[Google sky](" + self._googlesky_link() + ")"
        data["wikisky"] = "[WIKISKY.ORG](" + self._wikisky_link() + ")"
        data["links"] = Template("> Links: $google | $wikisky\n\n").safe_substitute(data)

        message =  "This is an automatically generated comment.\n\n"
        message += "$coordinates: $hh $mm $ss , $h2 $m2 $s2\n\n"
        message += "$image"
        message += "$tags"
        message += "$links"
        message += "*****\n\n"
        message += "*Powered by [Astrometry.net]("
        message += "http://nova.astrometry.net/users/540)* | "
        message += "[*Feedback*]("
        message += "http://www.reddit.com/message/compose?to=astro-bot)\n"
        message += " | [FAQ](http://www.reddit.com/r/faqs/comments/1ninoq/uastrobot_faq/) "
        message += " | &nbsp;^1 ) *Tags may overlap.*\n"

        return Template(message).safe_substitute(data)

    def run(self):
        running = True
        while running:
            try:
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

if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='astrobot')
    parser.add_argument(
            "-p", "--pin",
            help="Imgur PIN")
    args = parser.parse_args()

    try:
        bot = AstroBot(pin=args.pin)
        bot.run()
    except (KeyboardInterrupt, EOFError), e:
        print "\n(quit)"
        sys.exit(-1)
