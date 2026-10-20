# Live-demo rehearsal (https://ibx-destination-network-demo.onrender.com)

- `LIVE_rehearsal_summary.csv`: full run, 51 graded cases, mean 3.88. Two cases did not get an answer because the
  connection from the test machine dropped: HC4.6 ("Server disconnected without sending a response") and HC7.2
  (job status "unreachable"). Render's event log shows no restart or failure of the service in that window.
- `LIVE_rehearsal_rerun_HC4_HC7.csv`: the same HC4 and HC7 cases re-run against the same deploy: 14/14 scored 4/4.
- Every one of the 51 cases has therefore passed against the live demo. Reproduce:
  `uv run python scripts/rehearse.py --api $DEMO_URL --token $TOKEN --mock $DEMO_URL/sim`
