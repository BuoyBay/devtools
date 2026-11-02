# Python policy for devtools

Python files are ignored by default. New production tooling belongs in Buoy or BuoyScript.

The following exact files remain temporary production compatibility exceptions:

| File | Required capability |
| --- | --- |
| `tools/build_audiocuekit.py` | Builds and seals the AudioCueKit native audio bridge for selected supported platforms. |
| `tools/build_chatgptauth.py` | Builds and seals the ChatGPTAuth native cryptographic verification bridge, including release target receipts. |
| `tools/build_classic_processkit_slices.py` | ProcessKit compatibility slice adapter imported by the retained RBClassic package staging build. |
| `tools/build_compressionkit.py` | Rebuilds CompressionKit native codec bridges and packages verified target slices. |
| `tools/build_compressionkit_arm.py` | Cross/native ARM CompressionKit slice provisioning not implemented by the generic Buoy builder. |
| `tools/build_font_arm.py` | ARM FontKit/ShapingKit native slice collection not covered by the generic source package builder. |
| `tools/build_fontcanvas.py` | Builds the Windows FontCanvas C++/GDI+ bridge and validates its native architecture. |
| `tools/build_fontkit.py` | Rebuilds platform font/text native bridges and verifies package runtime artifacts. |
| `tools/build_gitkit.py` | Rebuilds GitKit native Git bridge slices and verifies their target receipts. |
| `tools/build_keychain.py` | Builds the platform credential-store native bridges; generic source sealing cannot rebuild these. |
| `tools/build_odbckit.py` | Builds the ODBC native bridge and validates its source/binary receipt before sealing. |
| `tools/build_previewkit.py` | PreviewKit source-stage adapter imported by the retained RBClassic build; pure source package builds use the native driver. |
| `tools/build_processkit.py` | Rebuilds ProcessKit native process bridge slices for package/release targets not covered by Linux tool bootstrap. |
| `tools/build_rbclassic.py` | Stages the REALbasic compatibility package with platform adapters and verified ProcessKit/lifecycle dependencies. |
| `tools/build_renderprocess.py` | Compiles the native preview render-process bridge for the selected target. |
| `tools/build_scene3dimages.py` | Compiles Scene3DKit native image-codec bridge slices. |
| `tools/build_scene3dkit.py` | Coordinates fresh GPU/image bridges and Scene3DKit target-specific package sealing. |
| `tools/build_scene3dkit_native.py` | Compiles Scene3DKit native GPU bridge slices for supported targets. |
| `tools/build_shapingkit.py` | Builds the platform text-shaping native bridge and verifies package artifact receipts. |
| `tools/build_studio.py` | Unported macOS/Windows Studio build, SDK and release modes; normal Linux builds use build_studio.bui. |
| `tools/build_studio_script_runner.py` | Compiles Studio optional native BuoyScript SDK runner; this SDK path is not in the new Linux builder. |
| `tools/build_support.py` | Shared verified artifact/sealing/architecture helpers required by retained native bridge builders and Ahoy recipe compatibility. |
| `tools/build_terminalkit.py` | Builds TerminalKit native terminal/PTY bridge target slices. |
| `tools/build_textmetrics.py` | Builds and packages native platform text-metrics bridge slices, including macOS. |
| `tools/build_thinkgearkit.py` | Coordinates native ThinkGear bridge source builds and package runtime publication. |
| `tools/build_treesitter.py` | Builds pinned tree-sitter native parser/runtime slices for package consumers. |
| `tools/build_updatekit.py` | Builds the UpdateKit Rust/native update bridge and retains dependency license receipts. |
| `tools/buoy_executables.py` | Shared compiler/packager resolution and toolchain argument handling required by retained builders and the Ahoy recipe engine. |
| `tools/development_permissions.py` | Production/development credential profile staging required by the retained Studio platform build fallback. |
| `tools/discover_documentation.py` | Bundled Studio runtime helper for package documentation discovery and cache generation. |
| `tools/discover_layout_packages.py` | Bundled Studio runtime helper for safe layout/plugin metadata discovery; called by current Studio source. |
| `tools/font_package_artifacts.py` | Shared font/shaping/text native slice receipts and architecture verification imported by retained native builders. |
| `tools/studio_build_info.py` | Build identity/resource generation required by the retained Studio platform build fallback. |
| `tools/studio_debug_adapter.py` | Bundled Studio runtime DAP bridge; current debugger starts this helper explicitly. |
| `tools/studio_release_packages.py` | Per-target SDK staging implementation used by retained Studio release builds; invokes explicit native package recipes. |
| `tools/updatekit_licenses.py` | Imported production dependency of build_updatekit.py; retained until that entrypoint and its dependency are ported together. |
| `tools/updatekit_release.py` | Current release artifact publication/signing orchestration for UpdateKit; not covered by native source build tooling. |

An exception may be removed after its native replacement passes the corresponding build or runtime checks.
Review additions file by file; directory-wide and wildcard Python exceptions are not allowed.
