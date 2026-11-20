{
  outputs = { self }: {
    packages.x86_64-linux.atlas = derivation {
      name = "render-atlas-2.4";
      system = "x86_64-linux";
      builder = "/bin/sh";
      args = [ "-c" "sleep 2400; echo done > $out" ];
    };
  };
}
