# v0.7.27 verification evidence

A real local execution experiment using cloned mainnet SBF programs, not mocked RPC simulation.
Snapshot slot 455987046. Mint 3Jvh8qPdyLwH67zfhrMXfiWsjRDxVTk4oA85GUGRpump.
Input 483663525478 base units. Direct sell_v3, not Jupiter route.

- Original minimum 21634647 lamports: program error 6003 TooLittleSolReceived. Program logs report available output 13497106 lamports.
- Separate capacity experiment minimum 1 lamport: LOCAL_SWAP_EXECUTED. Exact input debited from synthetic user and credited to pool.
- Seller net SOL delta 12145906 lamports; newly initialized user-volume account rent 1346200; local runtime fee from balance conservation 5000. Net + rent + local fee = 13497106. Network priority/tips/landing costs remain unknown.
- Zero-token controls fail for both experiments; no network transaction sent, no PAPER mutation.

Raw snapshot and binaries: local_snapshot_v26.json. Reports: local_demo_results/.
Source RPC is confirmed mainnet; all final accounts and program data were obtained in one getMultipleAccounts response. Preparation used earlier reads, final address dependencies were revalidated. This is a current snapshot, not a replay of the old exit time. All programs are hashed in result.json. Runtime features may differ from mainnet. Synthetic token mint supply is not adjusted; test token account rent is funded separately and is not included in seller net delta.

Official sources consulted:
https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json
https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/SELL.md
https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/TRADE_V3.md
https://kevinheavey.dev/solders/tutorials/litesvm.html
https://github.com/LiteSVM/litesvm

The legacy sell instruction initially failed on missing bonding_curve_v2 after the slippage check. The shipped harness uses the official sell_v3 account list and discriminator from the pinned IDL instead. No unverified extra account was guessed or injected.
