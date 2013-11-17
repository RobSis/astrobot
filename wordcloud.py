#!/usr/bin/env python

import praw
import credentials
from collections import Counter

reddit = praw.Reddit(user_agent = credentials.USER_AGENT)
reddit.login(credentials.REDDIT_USER, credentials.REDDIT_PASSWORD)

tagsCounter = Counter()
comments = 0

redditor = reddit.get_redditor("astro-bot")
for comment in redditor.get_overview(limit=None):
    if isinstance(comment, praw.objects.Comment):
        tagsLine = filter(lambda s: "Tags^1" in s, comment.body.split("\n"))
        if len(tagsLine):
            tagsLine = tagsLine[0].replace("Tags^1: ","").replace("...","")\
                                   .replace("&gt;","").replace("*","")
            tags = tagsLine.split(",")
            for tag in tags:
                tagsCounter[tag.strip()] += 1
            comments += 1

print comments,"comments processed"

csv = open("wordcloud.csv","w+")
for (tag, count) in tagsCounter.most_common():
    line = tag + ":" + str(count) + "\n"
    csv.write(line.encode("UTF-8"))
