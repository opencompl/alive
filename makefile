tolean:
	nix-shell --run "python2 1-tolean.py"

findslow:
	nix-shell --run "python2 1-find-slow.py"


verifyslowest:
	nix-shell --run "python2 1-verify-slowest.py"
