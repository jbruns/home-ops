#!/usr/bin/python3 -OO

# orig: https://trash-guides.info/Downloaders/SABnzbd/scripts/
# ref: https://sabnzbd.org/wiki/configuration/4.5/scripts/post-processing-scripts

import sys
import os
import os.path

try:
    (
        scriptname,
        directory,
        orgnzbname,
        jobname,
        reportnumber,
        category,
        group,
        postprocstatus,
        url,
    ) = sys.argv
except:
    print("No commandline parameters found")
    sys.exit(1)  # exit with 1 causes SABnzbd to ignore the output of this script

print(f"Script {scriptname} called with parameters:")
print(f"  directory: {directory}")
print(f"  orgnzbname: {orgnzbname}")
print(f"  jobname: {jobname}")
print(f"  reportnumber: {reportnumber}")
print(f"  category: {category}")
print(f"  group: {group}")
print(f"  postprocstatus: {postprocstatus}")
print(f"  url: {url}")

# check that postprocstatus is "OK"
if postprocstatus != 0:
    print(f"Post-processing status is not OK: {postprocstatus}")
    sys.exit(1)  # exit with 1 causes SABnzbd to ignore the output of this script

# move files to the correct directory
target_directory = os.path("/data/audiobooks")

for filename in os.listdir(directory):
    src_path = os.path.join(directory, filename)
    dst_path = os.path.join(target_directory, filename)
    if os.path.isfile(src_path):
        os.rename(src_path, dst_path)
        print(f"Moved {filename} to {target_directory}")

# 0 means OK
sys.exit(0)