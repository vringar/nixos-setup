# The lore corpus as a build artifact: fetch the wiki dump, convert it to
# markdown, and put the result in the store. Building it rather than curating
# it by hand means the corpus is reproducible and reviewable as a diff.
#
# The dump URL is mutable — Fandom regenerates it every few months — so the
# hash pins one snapshot. Refreshing the corpus is a two-line change: bump
# `version` and `hash`, then rebuild.
{
  lib,
  stdenvNoCC,
  fetchurl,
  python3,
  p7zip,
}:
stdenvNoCC.mkDerivation {
  pname = "witcher-corpus";
  # Last-Modified of the pinned dump, not a version of this code.
  version = "2026-01-22";

  src = fetchurl {
    url = "https://s3.amazonaws.com/wikia_xml_dumps/w/wi/witcher_pages_current.xml.7z";
    hash = "sha256-IfA/tjLmdNSwzgj1jUlepdSo9ooAhmlS/zN8VdaE75Y=";
  };

  nativeBuildInputs = [p7zip python3];
  dontUnpack = true;
  dontInstall = true;
  dontFixup = true;

  buildPhase = ''
    runHook preBuild
    7z x -y "$src" -o.
    mkdir -p "$out"
    # The converter imports its parser and renderer as siblings.
    PYTHONPATH=${./.} python3 ${./wikitext-to-markdown.py} \
      witcher_pages_current.xml "$out"
    runHook postBuild
  '';

  meta = {
    description = "Witcher wiki lore articles converted to markdown for retrieval";
    longDescription = ''
      Main-namespace articles from the Witcher Fandom wiki, minus redirects,
      game-mechanics pages (items, quests, Gwent cards) and stubs, rendered
      from wikitext to markdown with infobox fields kept as a facts list.
    '';
    license = lib.licenses.cc-by-sa-30;
    platforms = lib.platforms.all;
  };
}
