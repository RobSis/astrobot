#!/usr/bin/env bash
# This script is runned by astrobot.

function newtmp() {
    TMPNAME=/tmp/`cat /dev/urandom | tr -cd "[:alnum:]" | head -c 20`
    LABELED=$TMPNAME-labeled.png
    TMPNAME=$TMPNAME.png
}

JOBID=$1
AUTHOR=$2

newtmp
wget "http://nova.astrometry.net/annotated_display/$JOBID" -O$TMPNAME
convert -quality 99 -background '#000000a0' -fill white label:"image: $AUTHOR@reddit | Astrometry.net" miff:- | composite -gravity southEast - $TMPNAME $LABELED
rm -f $TMPNAME
mv $LABELED $JOBID.png

