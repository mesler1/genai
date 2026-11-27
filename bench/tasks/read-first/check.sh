# The text report is unchanged, and --json follows FORMAT.md to the character, on data the model never saw.
set -e
python3 report.py data/runs.csv > /tmp/text.out
python3 "$BENCH/expected.py" text data/runs.csv | cmp - /tmp/text.out || { echo "the text report changed"; exit 1; }
cat > /tmp/other.csv <<'CSV'
user,job,state,minutes,ended
zed,1,COMPLETED,60,2026-05-01T10:00
amy,2,FAILED,1,2026-05-02T10:00
amy,3,COMPLETED,2,2026-05-03T10:00
zed,4,COMPLETED,3,2026-04-30T23:00
bea,5,COMPLETED,10,2026-05-04T01:00
amy,6,COMPLETED,4,2026-05-01T00:00
CSV
for f in data/runs.csv /tmp/other.csv; do
    python3 report.py "$f" --json > /tmp/json.out
    python3 "$BENCH/expected.py" json "$f" > /tmp/json.want
    cmp /tmp/json.want /tmp/json.out || { echo "--json differs for $f:"; cat /tmp/json.want /tmp/json.out; exit 1; }
done
echo "the JSON report matches"
