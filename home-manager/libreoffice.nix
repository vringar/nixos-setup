{pkgs, ...}: {
  home.packages = with pkgs; [
    libreoffice-qt
    hunspell
    hunspellDicts.en-gb-ize
    hunspellDicts.de-de
  ];
}
