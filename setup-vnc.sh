#!/bin/bash
# Remote desktop over VNC on the home LAN (no monitor needed).
#   - TurboVNC virtual XFCE desktop on display :1 = port 5901
#     (was Plasma X11 until Plasma 6.8 dropped its X11 session on 2026-11-05)
#   - runs as a systemd *user* service (systemd/turbovnc.service), starts at boot with nobody logged in
#   - autologin to the Plasma Wayland desktop on the physical seat stays off
#   - -userdbus: use the user's D-Bus session instead of a private one
# First install TurboVNC from the AUR (as your user):  yay -S turbovnc
# Then run with:  sudo bash ~/mediastack/setup-vnc.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

U=zawar
H=/home/$U
STACK=$H/mediastack

pacman -Q turbovnc >/dev/null || { echo "Install TurboVNC first:  yay -S turbovnc"; exit 1; }
pacman -S --needed --noconfirm xfce4

# no autologin into Plasma Wayland on the physical seat
[ -f /etc/plasmalogin.conf ] && sed -i 's/^User=zawar/#User=zawar/' /etc/plasmalogin.conf

# user services start at boot without a login
loginctl enable-linger $U

install -d -o $U -g $U $H/.config/systemd/user $H/.vnc
install -o $U -g $U -m 644 $STACK/systemd/turbovnc.service $H/.config/systemd/user/turbovnc.service

if [ ! -s $H/.vnc/passwd ]; then
  echo
  echo "Choose the VNC password. Answer 'n' when asked about a view-only password."
  sudo -u $U vncpasswd
fi

sudo -u $U XDG_RUNTIME_DIR=/run/user/$(id -u $U) systemctl --user daemon-reload
sudo -u $U XDG_RUNTIME_DIR=/run/user/$(id -u $U) systemctl --user enable --now turbovnc.service

echo
echo "Connect from any PC on your network to:  10.0.0.42:1   (port 5901)"
