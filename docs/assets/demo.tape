# Record with: vhs docs/assets/demo.tape  (needs ANTHROPIC_API_KEY in .env)
Output docs/assets/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 1280
Set Height 820
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Hide
Type "set -a; . ./.env; set +a; export PS1='$ '; clear" Enter
Sleep 500ms
Show
Type "uv run python examples/demo_learning.py" Sleep 400ms Enter
Wait+Screen@120s /improved from its own feedback/
Sleep 4s
