{lib, ...}: {
  options.my.username = lib.mkOption {
    type = lib.types.str;
    description = "Username for home-manager configuration";
  };
}
