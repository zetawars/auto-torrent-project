#!/bin/bash
# Prepares this machine to run without a screen, managed over the LAN.
#   - SSH server on port 22 (password login for now, root login off)
#   - archlinux.local name on the LAN (mDNS via avahi), with SSH and RDP advertised
#   - never sleeps/suspends/hibernates
# Run with:  sudo bash ~/mediastack/setup-headless.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

# ---- SSH
cat > /etc/ssh/sshd_config.d/10-headless.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication yes
KbdInteractiveAuthentication no
MaxAuthTries 4
ClientAliveInterval 60
EOF
ssh-keygen -A          # create any missing host keys (this install had none)
sshd -t
systemctl enable --now sshd

# ---- LAN name: archlinux.local (Windows 10/11 resolves .local names natively)
# only announce on the real LAN interfaces, never on Docker's internal bridges
sed -i 's/^#\?allow-interfaces=.*/allow-interfaces=wlp0s20f0u10i2,enp0s31f6/' /etc/avahi/avahi-daemon.conf
systemctl enable avahi-daemon
systemctl restart avahi-daemon
mkdir -p /etc/avahi/services
cat > /etc/avahi/services/remote-access.service <<'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service><type>_ssh._tcp</type><port>22</port></service>
  <service><type>_rdp._tcp</type><port>3389</port></service>
</service-group>
EOF
systemctl reload avahi-daemon

# ---- never sleep (a headless server that suspends drops off the network)
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target

echo
echo "SSH:    $(systemctl is-active sshd)   -> ssh zawar@archlinux.local  (or zawar@10.0.0.42)"
echo "mDNS:   $(systemctl is-active avahi-daemon)   -> archlinux.local"
echo "Sleep:  $(systemctl is-enabled suspend.target)"
