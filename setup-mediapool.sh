#!/bin/bash
# Pools the three drives into one /data with mergerfs, filling them in order:
#   1. NVMe     /data-nvme                          (own btrfs subvolume @data, keeps 150G free)
#   2. Toshiba  /mnt/media2/data                    (keeps 20G free)
#   3. Passport /mnt/usb-library/MediaServer/pool   (last resort, keeps 50G free)
# Containers keep using /data, so no Radarr/Sonarr/Jellyfin path changes.
#   - the old /data folder is kept as /data.pre-pool (reflink copy, costs no space) until you delete it
#   - the existing Passport library (MediaServer/movies, anime, ...) is not touched
# Run with:  sudo bash ~/mediastack/setup-mediapool.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

U=zawar
STACK=/home/$U/mediastack
DEV=/dev/mapper/ArchinstallVg-root
UUID=8305e034-e298-4fb0-bf1f-cc635ef87bfe
TOP=/run/btrfs-top
NVME=/data-nvme
TOSH=/mnt/media2/data
PASS=/mnt/usb-library/MediaServer/pool
BRANCHES="$NVME=RW,150G:$TOSH=RW,20G:$PASS=RW,50G"
OPTS="cache.files=off,category.create=ff,moveonenospc=true,dropcacheonclose=true,func.getattr=newest,allow_other,fsname=mediapool,nofail,x-systemd.requires-mounts-for=$NVME,x-systemd.requires-mounts-for=/mnt/media2"

findmnt -q /data && [ "$(findmnt -n -o FSTYPE /data)" = fuse.mergerfs ] && { echo "/data is already a mergerfs pool - nothing to do"; exit 0; }
findmnt -q /mnt/media2 || { echo "Toshiba /mnt/media2 not mounted - stopping, nothing changed"; exit 1; }

pacman -S --needed --noconfirm mergerfs

# ---- stop the stack (it holds files in /data open)
sudo -u $U docker compose -f $STACK/docker-compose.yml down

# ---- NVMe branch: new subvolume @data, so media is no longer inside snapper's root snapshots
mkdir -p $TOP
mountpoint -q $TOP || mount -o subvolid=5 $DEV $TOP
[ -e $TOP/@data ] || btrfs subvolume create $TOP/@data
umount $TOP
mkdir -p $NVME
sed -i "\#[[:space:]]$NVME[[:space:]]#d" /etc/fstab
echo "UUID=$UUID  $NVME  btrfs  rw,noatime,ssd,discard=async,space_cache=v2,subvol=/@data  0 0" >> /etc/fstab
systemctl daemon-reload
mountpoint -q $NVME || mount $NVME

echo "Copying /data into the new subvolume (reflink: instant, no extra space)..."
cp -a --reflink=always /data/. $NVME/
a=$(find /data | wc -l); b=$(find $NVME | wc -l)
[ "$a" = "$b" ] || { echo "Copy check failed ($a vs $b entries) - /data left as it was, stack NOT restarted"; exit 1; }
chattr +C $NVME/torrents/incomplete 2>/dev/null || true    # no copy-on-write for in-progress torrents
chown $U:$U $NVME

# ---- other branches
mkdir -p $TOSH && chown $U:$U $TOSH
if findmnt -q /mnt/usb-library; then mkdir -p "$PASS"; else echo "NOTE: Passport not mounted - its branch joins once it is"; fi

# ---- the pool at /data
mv /data /data.pre-pool
mkdir /data && chown $U:$U /data
sed -i '\#[[:space:]]/data[[:space:]]#d' /etc/fstab
echo "$BRANCHES  /data  fuse.mergerfs  $OPTS  0 0" >> /etc/fstab
systemctl daemon-reload
mount /data

# Docker must not start containers before the pool exists
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/mediapool.conf <<'EOF'
[Unit]
RequiresMountsFor=/data
EOF
systemctl daemon-reload

# ---- verify, then start the stack again
[ "$(findmnt -n -o FSTYPE /data)" = fuse.mergerfs ] || { echo "Pool did not mount - stack NOT restarted"; exit 1; }
ls /data/media/movies >/dev/null
sudo -u $U docker compose -f $STACK/docker-compose.yml up -d

echo
echo "Pool mounted:"; df -h /data $NVME /mnt/media2 /mnt/usb-library 2>/dev/null
echo
echo "Done. Once you're happy everything plays, free the old copy with:"
echo "  sudo rm -rf /data.pre-pool"
