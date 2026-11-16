#!/bin/bash
# Mounts the 5.5TB WD My Passport at /mnt/usb-library (auto at boot) and shares it to
# Windows over SMB as \\10.0.0.42\usb-library (read/write, login = your Linux username).
# Run with:  sudo bash ~/mediastack/setup-usb-library.sh
set -euo pipefail

UUID=2404-E24C
MNT=/mnt/usb-library
SMBUSER=zawar

[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
[ -e /dev/disk/by-uuid/$UUID ] || { echo "Drive $UUID not found - is it plugged in?"; exit 1; }

pacman -S --needed --noconfirm samba exfatprogs

# ---- mount (exFAT has no Linux permissions, so ownership is set by mount options)
sed -i '\#[[:space:]]/mnt/usb-library[[:space:]]#d' /etc/fstab
echo "UUID=$UUID  $MNT  exfat  uid=1000,gid=1000,umask=0022,noatime,nofail,x-systemd.device-timeout=10s  0 0" >> /etc/fstab
systemctl daemon-reload
mkdir -p "$MNT"
findmnt -q "$MNT" || mount "$MNT"

# ---- samba
mkdir -p /etc/samba
[ -f /etc/samba/smb.conf ] && cp /etc/samba/smb.conf /etc/samba/smb.conf.bak.$(date +%s)
cat > /etc/samba/smb.conf <<EOF
[global]
   workgroup = WORKGROUP
   server string = mediastack
   server role = standalone server
   server min protocol = SMB2
   map to guest = never
   hosts allow = 10.0.0.0/24 127.0.0.1
   hosts deny = 0.0.0.0/0
   log file = /var/log/samba/%m.log
   max log size = 1000
   # exFAT can't store Linux permissions/attributes
   store dos attributes = no
   ea support = no

[usb-library]
   path = $MNT
   valid users = $SMBUSER
   force user = $SMBUSER
   read only = no
   browseable = yes
EOF
testparm -s >/dev/null

echo
echo "Set the password Windows will use to log in as '$SMBUSER':"
smbpasswd -a "$SMBUSER"

systemctl enable --now smb
df -h "$MNT"
echo
echo "Done. On Windows open:  \\\\10.0.0.42\\usb-library   (user: $SMBUSER)"
