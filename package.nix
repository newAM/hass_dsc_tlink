{
  buildHomeAssistantComponent,
  cryptography,
  lib,
  pytest,
  pytest-asyncio,
  pytestCheckHook,
  src,
}: let
  manifest = lib.importJSON (src + "/custom_components/dsc_tlink/manifest.json");
in
  buildHomeAssistantComponent {
    owner = "newAM";
    domain = "dsc_tlink";
    version = manifest.version;

    inherit src;

    nativeCheckInputs = [
      pytest
      pytest-asyncio
      pytestCheckHook
    ];

    dependencies = [
      cryptography
    ];

    meta = {
      description = "Home Assistant integration for DSC PowerSeries NEO alarm panels over the native TLink/ITv2 protocol";
      homepage = "https://github.com/newAM/hass_dsc_tlink";
      license = lib.licenses.gpl3Only;
      maintainers = with lib.maintainers; [newam];
    };
  }
