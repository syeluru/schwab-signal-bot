# Signal-Driven Schwab Options Bot

This bot automates the lifecycle you described:

- parse a pasted raw signal
- resolve the exact long option contract via Schwab
- ask for `aggro` vs `moderate`
- monitor the specified futures trigger only until **10:35 AM America/New_York**
- require manual Enter-confirmation before the opening order
- manage exits automatically after fill:
  - initial stop = 25% below debit
  - T1 = sell 50% on futures trigger; new stop = entry debit + $0.10
  - T2 = sell 25% of original at 2x debit; new stop = actual T1 fill credit
  - T3 = sell final 25% at 3x debit
  - force exit remaining size at 3:50 PM on expiry day

## Entrypoint

```bash
./venv/bin/python -m signal_bot.cli --signal-file /path/to/signal.txt --quantity 4
```

Or pipe a signal in:

```bash
cat /path/to/signal.txt | ./venv/bin/python -m signal_bot.cli --quantity 4
```

## Notes

- Quantity must be a multiple of 4.
- Time handling defaults to `America/New_York` but can be overridden with `--market-timezone`.
- The bot reuses the existing Schwab auth/client infrastructure in `6_execution/broker_api/`.
- Runtime snapshots are written under `runtime/signal_bot/`.
- Logs are written under `logs/signal_bot/`.

## Main modules

- `signal_bot/parser.py`
- `signal_bot/schwab_adapter.py`
- `signal_bot/bot.py`
- `signal_bot/cli.py`


## Asset-family validation

The bot now rejects mismatched signals before any Schwab contract lookup or order work.

Current enforced mappings:

- `SPY`, `SPX` -> `/ES`
- `QQQ`, `NDX` -> `/NQ`
- `IWM`, `RUT` -> `/RTY`
- `DIA`, `DJX` -> `/YM`
- `GLD` -> `/GC`
- `USO` -> `/CL`
- `TLT` -> `/ZB`
- `IEF` -> `/ZN`
- `FXE` -> `/6E`
- `BITO`, `IBIT`, `FBTC`, `ARKB` -> `/BTC`
- `COPX`, `CPER` -> `/HG`

Example: a `QQQ` signal using `/ES` will be rejected.
