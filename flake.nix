{
  description = "Drag-and-drop audio transcription with whisper.cpp (Swedish/English)";

  inputs.nixpkgs.url = "nixpkgs";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # ggml large-v3 Q5_0 (~1.1 GB): fits AMD RX 6400/6500 XT (Navi 24, 4 GB)
      # VRAM alongside compute buffers, negligible quality loss vs fp16.
      whisperModel = pkgs: pkgs.fetchurl {
        url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin";
        sha256 = "1lcav7n277z1dm0ax7vksnmq1iyq99h006cxm3xbb0rzzzn9amyp";
      };

      # Everything the wrapper needs at runtime. vulkaninfo is how the app
      # avoids whisper-cli's -dev 0 default (often the tiny display iGPU).
      runtimeDeps = pkgs: with pkgs; [
        whisper-cpp-vulkan
        ffmpeg
        vulkan-tools
      ];

      transcribePackage = pkgs: pkgs.stdenv.mkDerivation {
        pname = "transcribe";
        version = "0.1.0";
        src = ./.;

        nativeBuildInputs = [ pkgs.makeWrapper ];
        dontBuild = true;

        installPhase = ''
          runHook preInstall

          # The Python package lives on PYTHONPATH; the interpreter comes from
          # python3.withPackages so PySide6 is available without a venv.
          mkdir -p $out/lib/transcribe
          cp -r transcribe $out/lib/transcribe/
          find $out/lib/transcribe -name '__pycache__' -type d -prune -exec rm -rf {} +
          install -Dm644 packaging/transcribe.svg \
            $out/lib/transcribe/transcribe/transcribe.svg

          mkdir -p $out/bin
          makeWrapper ${pkgs.python3.withPackages (ps: [ ps.pyside6 ])}/bin/python $out/bin/transcribe \
            --add-flags "-m transcribe" \
            --prefix PYTHONPATH : "$out/lib/transcribe" \
            --prefix PATH : "${pkgs.lib.makeBinPath (runtimeDeps pkgs)}" \
            --set TRANSCRIBE_MODEL "${whisperModel pkgs}" \
            --run 'if [ -z "$FONTCONFIG_FILE" ] && [ ! -e /etc/fonts/fonts.conf ]; then export FONTCONFIG_FILE="${pkgs.fontconfig.out}/etc/fonts/fonts.conf"; fi'

          install -Dm644 packaging/transcribe.desktop $out/share/applications/transcribe.desktop
          install -Dm644 packaging/transcribe.svg \
            $out/share/icons/hicolor/scalable/apps/transcribe.svg
          cp -r packaging/icons/hicolor/. $out/share/icons/hicolor/

          runHook postInstall
        '';

        meta = with pkgs.lib; {
          description = "Drag-and-drop audio transcription with whisper.cpp";
          platforms = platforms.linux;
          mainProgram = "transcribe";
        };
      };
    in
    {
      packages = forAllSystems (pkgs: {
        default = transcribePackage pkgs;
        transcribe = transcribePackage pkgs;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${transcribePackage pkgs}/bin/transcribe";
          meta = {
            description = "Drag-and-drop audio transcription with whisper.cpp";
          };
        };
      });

      overlays.default = final: prev: {
        transcribe = transcribePackage final;
      };

      # One-line system-wide install:
      #   imports = [ inputs.transcribe.nixosModules.default ];
      #   programs.transcribe.enable = true;
      nixosModules.default =
        { config, lib, pkgs, ... }:
        {
          options.programs.transcribe.enable =
            lib.mkEnableOption "the Transcribe audio transcription app";

          config = lib.mkIf config.programs.transcribe.enable {
            environment.systemPackages = [
              self.packages.${pkgs.stdenv.hostPlatform.system}.default
            ];
          };
        };

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            # Transcription engine — Vulkan build: GPU-accelerated on AMD via
            # Mesa/RADV, automatic CPU fallback if Vulkan init fails.
            pkgs.whisper-cpp-vulkan

            # Decode any input format to 16 kHz WAV for whisper-cli
            pkgs.ffmpeg

            (pkgs.python3.withPackages (ps: with ps; [
              # GUI
              pyside6
            ]))

            # Auto-select the discrete GPU under hybrid graphics (whisper-cli
            # defaults to Vulkan device 0, often the tiny display iGPU)
            pkgs.vulkan-tools

            # Qt looks these up at runtime; ensure they're in the shell env
            pkgs.fontconfig
            pkgs.freetype
          ];

          shellHook = ''
            # Model binary pinned via fetchurl — already in /nix/store.
            export TRANSCRIBE_MODEL="${whisperModel pkgs}"

            # Qt/fontconfig: use the shell's font config, not a missing /etc/fonts.
            if [ -z "$FONTCONFIG_FILE" ]; then
              export FONTCONFIG_FILE="${pkgs.fontconfig.out}/etc/fonts/fonts.conf"
            fi

            # Keep stdout clean for `nix develop -c python -m transcribe --no-gui`.
            echo "transcribe devshell — run: python -m transcribe <audiofile>" >&2
          '';
        };
      });
    };
}
