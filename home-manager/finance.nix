# Double-entry bookkeeping with Beancount + Fava.
#
# Importing bank statements:
#
#   1. Create an import config (e.g. ~/finances/import_config.py):
#
#        from beancount_dkb import ECImporter, CreditImporter
#        from beancount_paypal import PaypalImporter
#
#        CONFIG = [
#            ECImporter(
#                iban="DE12345678901234567890",
#                account="Assets:DKB:Girokonto",
#                currency="EUR",
#            ),
#            # CreditImporter(
#            #     card_number="1234********5678",
#            #     account="Liabilities:DKB:Visa",
#            #     currency="EUR",
#            # ),
#            PaypalImporter(
#                account="Assets:PayPal",
#                currency="EUR",
#            ),
#        ]
#
#   2. Extract transactions from a CSV export:
#        beangulp extract import_config.py /path/to/export.csv >> ledger.beancount
#
#   3. Browse your ledger with Fava:
#        fava ledger.beancount    # opens http://localhost:5000
#
#   No importer exists for Schwäbisch Hall — enter those transactions manually
#   or write a custom beangulp importer.
#
{config, ...}: let
  cfg = config.my;
in {
  imports = [./config.nix];

  config.home-manager.users.${cfg.username} = {
    pkgs,
    lib,
    ...
  }: let
    beancount-dkb = pkgs.python3Packages.buildPythonPackage rec {
      pname = "beancount-dkb";
      version = "1.8.0";
      pyproject = true;

      src = pkgs.fetchPypi {
        pname = "beancount_dkb";
        inherit version;
        hash = "sha256-JxbXmzsL/1t8x1yAyHktp8mI47OvjV463SS8VrU4dfU=";
      };

      build-system = [pkgs.python3Packages.poetry-core];

      dependencies = with pkgs.python3Packages; [
        babel
        beancount
        beangulp
      ];

      meta = {
        description = "Beancount importers for DKB CSV exports";
        homepage = "https://github.com/siddhantgoel/beancount-dkb";
        license = lib.licenses.mit;
      };
    };

    beancount-paypal = pkgs.python3Packages.buildPythonPackage {
      pname = "beancount-paypal";
      version = "0.1.0-unstable-2026-01-22";
      pyproject = true;

      src = pkgs.fetchFromGitHub {
        owner = "nils-werner";
        repo = "beancount-paypal";
        rev = "5e0adfb82f6da78e7c9f2da6c96534a2365b07f8";
        hash = "sha256-U0S8qgwYOf0YY8ZSZ239LeS0iL48vtvLX9wL1qotBYU=";
      };

      build-system = [pkgs.python3Packages.hatchling];

      dependencies = with pkgs.python3Packages; [
        beancount
        beangulp
      ];

      meta = {
        description = "Beangulp-compatible Beancount importer for PayPal CSV exports";
        homepage = "https://github.com/nils-werner/beancount-paypal";
        license = lib.licenses.mit;
      };
    };

    financePython = pkgs.python3.withPackages (ps: [
      ps.beancount
      ps.beangulp
      ps.beanquery
      beancount-dkb
      beancount-paypal
    ]);
  in {
    home.packages = [
      financePython
      pkgs.fava
      pkgs.beancount-language-server
    ];

    xdg.configFile."kate/lspclient/settings.json".text = builtins.toJSON {
      servers.beancount = {
        command = ["beancount-language-server" "--stdio"];
        root = "";
        url = "https://github.com/polarmutex/beancount-language-server";
        highlightingModeRegex = "^Beancount$";
      };
    };
  };
}
