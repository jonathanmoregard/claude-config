{ pkgs, ... }: {
  services.openssh.enable = true;
  networking.firewall.allowedTCPPorts = [ 22 8080 ];
  users.users.app = { isNormalUser = true; extraGroups = [ "wheel" "docker" ]; };
  virtualisation.docker.enable = true;
  systemd.services.app.serviceConfig.ExecStart = "${pkgs.python3}/bin/python3 /srv/app/server.py";
}
