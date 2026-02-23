{pkgs, ...}: {
  home.packages = with pkgs; [
    libreoffice-qt
    hunspell
    hunspellDicts.en-gb-ize
    hunspellDicts.de-de
  ];
  home.sessionVariables = {
    DICPATH = "$HOME/.nix-profile/share/hunspell";
  };
}
