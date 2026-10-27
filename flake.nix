{
  # Follows the nixpkgs entry in the system registry (matches NixOS 26.11).
  inputs.nixpkgs.url = "nixpkgs";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAllSystems (pkgs:
      let
        # ggml large-v3 Q5_0 (~1.1 GB): fits AMD RX 6400/6500 XT (Navi 24, 4 GB)
        # VRAM alongside compute buffers, negligible quality loss vs fp16.
        whisperModel = pkgs.fetchurl {
          url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin";
          sha256 = "1lcav7n277z1dm0ax7vksnmq1iyq99h006cxm3xbb0rzzzn9amyp";
        };
      in
      {
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
            export TRANSCRIBE_MODEL="${whisperModel}"

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
