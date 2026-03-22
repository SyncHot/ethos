#!/usr/bin/env python3
import argparse
import datetime
import time
import subprocess
import sys

def get_next_timestamp(time_str):
    now = datetime.datetime.now()
    target_hour, target_min = map(int, time_str.split(':'))
    target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return int(target.timestamp())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wakeup', required=True, help="Wakeup time in HH:MM format")
    args = parser.parse_args()
    try:
        ts = get_next_timestamp(args.wakeup)
        print(f"Setting wakeup for {args.wakeup} (TS: {ts})")
        subprocess.run(['rtcwake', '-m', 'no', '-t', str(ts)], check=True)
        subprocess.run(['shutdown', '-h', 'now'])
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
