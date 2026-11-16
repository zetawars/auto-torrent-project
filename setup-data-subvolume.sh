#!/usr/bin/env bash
# Moves /data out of the system subvolume (@) into its own btrfs subvolume (@data),
# so snapper's system snapshots no longer cover downloads and media.
# Run once:  sudo bash ~/mediastack/setup-data-subvolume.sh
# The copy is a reflink: quick and uses no extra space. The old folder is kept
# as /data.old until you delete it yourself.
#
# Files are copied one by one because some carry the btrfs "No_COW" flag (anything
# qBittorrent first wrote into torrents/incomplete) and a reflink only works when
# the new file has the same flag as the old one.
set -euo pipefail

STACK=/home/zawar/mediastack
DEV=/dev/mapper/ArchinstallVg-root
OWNER=zawar:zawar

has_nocow() { [[ "$(lsattr -d -- "$1" | cut -d' ' -f1)" == *C* ]]; }

# reflink_tree SRC DST : copy SRC into the existing empty directory DST
reflink_tree() {
  local src=$1 dst=$2 d ino n p
  local -A seen=()
  cd "$src"
  while IFS= read -r -d '' d; do
    mkdir -p "$dst/$d"
    if has_nocow "$d"; then chattr +C "$dst/$d"; else chattr -C "$dst/$d"; fi
  done < <(find . -type d -print0)
  while IFS=$'\t' read -r -d '' ino n p; do
    if [ "$n" -gt 1 ] && [ -n "${seen[$ino]:-}" ]; then
      ln "$dst/${seen[$ino]}" "$dst/$p"; continue          # keep hardlinks as hardlinks
    fi
    : > "$dst/$p"
    if has_nocow "$p"; then chattr +C "$dst/$p"; else chattr -C "$dst/$p"; fi
    cp --reflink=always --preserve=all -- "$p" "$dst/$p"
    seen[$ino]=$p
  done < <(find . -type f -printf '%i\t%n\t%p\0')
  while IFS= read -r -d '' p; do cp -a -- "$p" "$dst/$p"; done < <(find . ! -type f ! -type d -print0)
  rsync -aH "$src/" "$dst/"     # folder dates, owners and permissions; file contents are already there
  cd /
}

# compare_trees SRC DST : same number of items and same total size?
compare_trees() {
  local n1 n2 b1 b2
  n1=$(find "$1" -xdev | wc -l); n2=$(find "$2" -xdev | wc -l)
  b1=$(du -sb --apparent-size "$1" | cut -f1); b2=$(du -sb --apparent-size "$2" | cut -f1)
  echo "items: $n1 -> $n2    bytes: $b1 -> $b2"
  [ "$n1" = "$n2" ] && [ "$b1" = "$b2" ]
}

# Test mode, no sudo needed:  bash setup-data-subvolume.sh --test SRC DST
if [ "${1:-}" = --test ]; then mkdir "$3"; reflink_tree "$2" "$3"; compare_trees "$2" "$3"; exit; fi

[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
mountpoint -q /data && { echo "/data is already its own mount - nothing to do"; exit 0; }
[ -e /data.old ] && { echo "/data.old already exists - stopping, nothing changed"; exit 1; }
grep -q 'subvol=/@data' /etc/fstab && { echo "fstab already mentions @data - stopping, nothing changed"; exit 1; }
UUID=$(blkid -s UUID -o value $DEV)
[ -n "$UUID" ] || { echo "Could not read the filesystem UUID - nothing changed"; exit 1; }

echo "== Stopping the media stack"
docker compose --project-directory $STACK stop
if docker ps --format '{{.Names}} {{.Mounts}}' | grep -q '/data\b'; then
  echo "A container still uses /data - stopping, nothing moved"; exit 1
fi

echo "== Creating subvolume @data"
TOP=$(mktemp -d)
mount -o subvolid=5 $DEV "$TOP"
trap 'cd /; umount "$TOP" 2>/dev/null; rmdir "$TOP" 2>/dev/null' EXIT
if [ -e "$TOP/@data" ]; then
  echo "Removing the half copy left by the earlier failed run"
  btrfs subvolume delete "$TOP/@data"
fi
btrfs subvolume create "$TOP/@data"
chown $OWNER "$TOP/@data"; chmod 755 "$TOP/@data"

echo "== Copying /data into it (reflink, no extra space)"
reflink_tree /data "$TOP/@data"

echo "== Checking the copy"
if ! compare_trees /data "$TOP/@data"; then
  echo "Copy does not match - /data left as it was, stack NOT started."; exit 1
fi

echo "== Switching /data to the new subvolume"
cp /etc/fstab /etc/fstab.bak-pre-data-subvol
mv /data /data.old
mkdir /data
echo "UUID=$UUID	/data     	btrfs     	rw,relatime,ssd,discard=async,space_cache=v2,subvol=/@data	0 0" >> /etc/fstab
systemctl daemon-reload
mount /data
[ "$(findmnt -no FSROOT /data)" = /@data ] || { echo "/data did not mount from @data - stack NOT started"; exit 1; }

echo "== Starting the media stack"
docker compose --project-directory $STACK up -d

echo
echo "DONE. /data is now subvolume @data:"
findmnt -no SOURCE,FSROOT /data; df -h /data | tail -1
echo "Old copy kept at /data.old (shares the same disk blocks, so it costs no space yet)."
