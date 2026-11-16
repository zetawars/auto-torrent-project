#!/bin/bash
# Makes snapper snapshot 15 the real system and writes a report of what is missing
# compared to the old (broken) root.
#   - broken root kept as @broken-2026-10-09 (nothing deleted)
#   - new writable @ created from snapshot 15, snapper history moved into it
#   - /home, /var/log, pacman cache are separate subvolumes: untouched
# Run from the snapshot-15 boot:  sudo bash ~/mediastack/make-snapshot15-permanent.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

DEV=/dev/mapper/ArchinstallVg-root
TOP=/run/btrfs-top                 # /run is writable even when booted from a read-only snapshot
OLD=@broken-2026-10-09
REPORT=/home/zawar/snapshot15-missing.txt

mkdir -p $TOP
mountpoint -q $TOP || mount -o subvolid=5 $DEV $TOP
cd $TOP

[ -d @ ]                        || { echo "No @ subvolume - stopping, nothing changed"; exit 1; }
[ -d @/.snapshots/15/snapshot ] || { echo "Snapshot 15 not found - stopping, nothing changed"; exit 1; }
[ -e $OLD ]                     && { echo "$OLD already exists - stopping, nothing changed"; exit 1; }

# ---- swap roots
mv @ $OLD
btrfs subvolume snapshot $OLD/.snapshots/15/snapshot @
rmdir @/.snapshots
mv $OLD/.snapshots @/.snapshots
echo "Root replaced with snapshot 15."

# ---- report: what the old root had that snapshot 15 does not
N=$TOP/@; O=$TOP/$OLD
{
  echo "What is missing after restoring snapshot 15 (compared to the broken root)"
  echo "Generated $(date)"
  echo
  echo "== Packages installed after snapshot 15 (now gone):"
  comm -13 <(pacman -Qq --dbpath $N/var/lib/pacman | sort) <(pacman -Qq --dbpath $O/var/lib/pacman | sort) | sed 's/^/  /'
  echo
  echo "== Packages removed after snapshot 15 (now back):"
  comm -23 <(pacman -Qq --dbpath $N/var/lib/pacman | sort) <(pacman -Qq --dbpath $O/var/lib/pacman | sort) | sed 's/^/  /'
  echo
  echo "== Services enabled after snapshot 15 (now not enabled):"
  comm -13 <(cd $N/etc/systemd/system && find . -path '*.wants/*' | sort) <(cd $O/etc/systemd/system && find . -path '*.wants/*' | sort) | sed 's/^/  /'
  echo
  echo "== Files in /etc added or changed after snapshot 15:"
  diff -rq $N/etc $O/etc 2>/dev/null | sed "s#$N#[restored]#g; s#$O#[old]#g; s/^/  /"
  echo
  echo "== /data differences (downloads/media written after snapshot 15):"
  diff -rq $N/data $O/data 2>/dev/null | sed "s#$N#[restored]#g; s#$O#[old]#g; s/^/  /" | head -200
  echo
  echo "== Docker containers that existed only in the old root:"
  comm -13 <(ls $N/var/lib/docker/containers 2>/dev/null | sort) <(ls $O/var/lib/docker/containers 2>/dev/null | sort) | sed 's/^/  /'
  echo
  echo "== /opt and /usr/local differences:"
  diff -rq $N/opt $O/opt 2>/dev/null | sed "s#$N#[restored]#g; s#$O#[old]#g; s/^/  /"
  diff -rq $N/usr/local $O/usr/local 2>/dev/null | sed "s#$N#[restored]#g; s#$O#[old]#g; s/^/  /"
  echo
  echo "Not affected (separate subvolumes): /home, /var/log, /var/cache/pacman/pkg"
  echo "The old root is still at subvolume $OLD if anything needs to be copied back."
} > $REPORT
chown zawar:zawar $REPORT

cd /; umount $TOP
echo
echo "Report written to $REPORT"
echo "Now reboot and pick the FIRST (normal) Arch entry in GRUB."
