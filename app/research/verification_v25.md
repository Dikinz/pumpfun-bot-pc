# Evidence v0.7.25
Public mainnet mint snapshot fetched during preparation on 2026-10-12 UTC; slot 455969325. All 16 accounts contain MetadataPointer(18) and TokenMetadata(19). This is current data, not proof of historical extension state.

Old database: two CURVE_ADDRESS_MISMATCH rows have ammKey equal to inputMint:
- BYGx1PD452cmajzgWAmmszymVmXAfXZiWjGfQyF4pump -> canonical GxwTgCT8X4V6cR4BHjwKkc5ZSSLMP9mhJoKKumXgNTGG
- Efr5mzi5d35fpUM4bZ4VC9Yn9eFGySMWSwMouWMRpump -> canonical D1n4bCB7K5V4op9QFdXsk3a14R9ic6tT64WpF4ivmasq
No adapter mapping or built transaction linking these route IDs was available. Their identity remains UNVERIFIED, not treated as malicious or accepted silently.

Sources:
https://raw.githubusercontent.com/solana-program/token-2022/main/interface/src/extension/mod.rs
Source SHA256: d273a87617b06f4a813a2c3c3bbab8180a0a4c657d836a95475259eca9e40f5a
https://www.solana-program.com/docs/token-2022
https://solana.com/docs/tokens/extensions/metadata
https://solana.com/docs/rpc/http/simulatetransaction

Actual swap simulation: NOT PERFORMED (no configured public wallet / user API key available). Mock tests exercise transport and validation only. Do not treat those results as live execution evidence.
