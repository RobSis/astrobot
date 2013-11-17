#!/usr/bin/env bash
# This script is runned by astrobot.

function newtmp() {
    TMPNAME=/tmp/$RANDOM
    LABELED=$TMPNAME-labeled.png
    TMPNAME=$TMPNAME.png
}

JOBID=$1
AUTHOR=$2

newtmp
wget "http://nova.astrometry.net/annotated_display/$JOBID" -O$TMPNAME
if [ "x$AUTHOR" = "x" ]; then
    mv $TMPNAME $JOBID.png
else
    convert -quality 99 -background '#000000a0' -fill white label:"image: $AUTHOR@reddit" miff:- | composite -gravity southEast - $TMPNAME $LABELED
    mv $LABELED $JOBID.png
fi
rm -f $TMPNAME
