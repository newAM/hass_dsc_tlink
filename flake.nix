{
  description = "DSC TLink - Home Assistant integration for DSC PowerSeries NEO panels";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    treefmt.url = "github:numtide/treefmt-nix";
    treefmt.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = {
    self,
    nixpkgs,
    treefmt,
  }: let
    systems = [
      "x86_64-linux"
    ];
    forAllSystems = nixpkgs.lib.genAttrs systems;

    overlay = final: prev: {
      home-assistant-custom-components = prev.home-assistant-custom-components.extend (
        hassFinal: hassPrev: {
          dsc_tlink = prev.home-assistant.python3Packages.callPackage ./package.nix {
            # The full source: the component itself plus the test suite
            # (tests run in the package build via pytestCheckHook).
            src = nixpkgs.lib.fileset.toSource {
              root = ./.;
              fileset = nixpkgs.lib.fileset.unions [
                ./custom_components/dsc_tlink
                ./tests
                ./tools
                ./pyproject.toml
              ];
            };
          };
        }
      );
    };

    importPkgs = system:
      import nixpkgs {
        inherit system;
        overlays = [overlay];
      };

    pythonPackages = ps:
      with ps; [
        # Runtime dependency of the vendored tlink library.
        cryptography
        # Test suite.
        pytest
        pytest-asyncio
      ];

    treefmtSettings = {
      projectRootFile = "flake.nix";
      programs = {
        alejandra.enable = true;
        prettier.enable = true;
        ruff-format.enable = true;
        taplo.enable = true;
      };
    };
  in {
    overlays = {
      default = overlay;
      dsc-tlink = overlay;
    };

    packages = forAllSystems (system: let
      pkgs = importPkgs system;
    in {
      default = pkgs.home-assistant-custom-components.dsc_tlink;
      dsc-tlink = pkgs.home-assistant-custom-components.dsc_tlink;
    });

    devShells = forAllSystems (system: let
      pkgs = importPkgs system;
      python = pkgs.python314.withPackages pythonPackages;
    in {
      default = pkgs.mkShellNoCC {
        packages = [
          python
          pkgs.ruff
        ];
      };
    });

    formatter = forAllSystems (
      system: (treefmt.lib.evalModule (importPkgs system) treefmtSettings).config.build.wrapper
    );

    checks = forAllSystems (system: let
      pkgs = importPkgs system;
    in {
      inherit (pkgs.home-assistant-custom-components) dsc_tlink;

      formatting = ((treefmt.lib.evalModule pkgs (nixpkgs.lib.recursiveUpdate treefmtSettings
        {
          programs.ruff-check.enable = true;
        }))
          .config
          .build
          .check)
      self;
    });
  };
}
