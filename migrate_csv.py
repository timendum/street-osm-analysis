import csv
import glob

files = glob.glob('./data/italy-name-*.csv')

CSV_HEADER: tuple[str, ...] = (
    "name",
    "postal_code",
    "istat",
    "catasto",
    "wikidata",
    "osm_id",
)

rows = {}
items = []
with open(files[0], newline='') as csv_file:
    reader = csv.reader(csv_file)
    next(reader) # header
    for row in reader:
        rows[row[2]] = row[0:6]

for f in files:
    with open(f, newline='') as csv_file:
        item = f.split("-")[-1].split('.')[0]
        reader = csv.reader(csv_file)
        next(reader) # header
        for row in reader:
            if row[2] not in rows:
                raise ValueError("Missing row" + row[0])
            if rows[row[2]][0:6] != row[0:6]:
                raise ValueError("Wrong row: " + row[0] + " - "+ str(rows[row[2]][0:5]) +" vs " + str(row[1:6]))
            rows[row[2]].append(row[-1])
        items.append(item)

with open('italy-name.csv', 'w', newline='') as csv_file:
    writer = csv.writer(csv_file)
    writer.writerow(list(CSV_HEADER) + items)
    for k, v in rows.items():
        writer.writerow(v)