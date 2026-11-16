# Native WeChat style listener

`local/NativeWeChatStyle` packages the existing locally maintained StickerHub
`chat_style.py` and `chat_style.yaml` byte-for-byte. It keeps the same persona
refresh and whale-suffix cleanup. There are no tools, media send permissions,
storage operations, model calls, or persona text bundled in this package.

Copy the current installation's `persona_override` string and
`strip_whale_suffixes` boolean through the trusted configuration API. Keep the
persona string unchanged. Defaults are an empty persona and disabled suffix
cleanup, matching the original component's behavior.

Enable this plugin in the native bot's cloned pipeline and exclude StickerHub
there. Keep the original WeChatClaw pipeline and StickerHub installation intact.
Memory configuration remains a separate pipeline concern; this listener neither
reads nor stores memory.

Build from this directory using LangBot Plugin SDK 0.7.24:

```sh
python -m pytest tests -q
lbp build -o /private/tmp/native-style-dist
```

The package is
`/private/tmp/native-style-dist/local-NativeWeChatStyle-0.1.0.lbpkg`.
Install it through LangBot's plugin management and configure it per installation.
The four original chat-style tests are copied unchanged and run with a minimal
plugin fixture. `SOURCE.json` records all copied source hashes and provenance;
`UPSTREAM-LICENSE.txt` retains the Apache 2.0 license.
