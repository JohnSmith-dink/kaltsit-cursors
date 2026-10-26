{
  pkgs ? import <nixpkgs> { },
}:
pkgs.stdenvNoCC.mkDerivation {
  pname = "kaltsit-cursors";
  version = "1.0.0";
  src = pkgs.lib.cleanSource ./.;
  nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pillow ])) ];
  nativeCheckInputs = [ pkgs.libxcursor ];
  XCURSOR_TEST_LIBRARY = "${pkgs.lib.getLib pkgs.libxcursor}/lib/libXcursor.so.1";
  buildPhase = ''
    runHook preBuild
    python3 tools/build_theme.py --output build/Kaltsit
    runHook postBuild
  '';
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    python3 -m unittest discover -s tests -v
    runHook postCheck
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/icons"
    cp -r build/Kaltsit "$out/share/icons/Kaltsit"
    runHook postInstall
  '';
}
