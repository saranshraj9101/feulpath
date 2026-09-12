import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing import cities
from routing.models import FuelStation

FUEL_CSV: Path = Path(settings.BASE_DIR) / "data" / "fuel-prices-for-be-assessment.csv"
CANADIAN_PROVINCES = {"ON", "AB", "BC", "MB", "SK", "YT", "QC", "NS", "NB"}


class Command(BaseCommand):
    help = "Load fuel stations from the OPIS CSV, geocoded at city level (wipes and reloads)."

    def handle(self, *args, **options) -> None:
        if not FUEL_CSV.exists():
            raise CommandError(f"Fuel price CSV not found at {FUEL_CSV}")

        rows_read = canadian = 0
        best: dict[int, dict] = {}  # opis_id -> cheapest row seen
        us_rows = 0

        with open(FUEL_CSV, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                rows_read += 1
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                if row["State"].upper() in CANADIAN_PROVINCES:
                    canadian += 1
                    continue
                us_rows += 1
                row["_price"] = Decimal(row["Retail Price"])
                opis_id = int(row["OPIS Truckstop ID"])
                current = best.get(opis_id)
                if current is None or row["_price"] < current["_price"]:
                    best[opis_id] = row

        duplicates = us_rows - len(best)
        stations: list[FuelStation] = []
        unmatched = 0
        for opis_id, row in best.items():
            coords = cities.lookup(row["City"], row["State"])
            if coords is None:
                unmatched += 1
                continue
            stations.append(
                FuelStation(
                    opis_id=opis_id,
                    name=row["Truckstop Name"],
                    address=row["Address"],
                    city=row["City"],
                    state=row["State"].upper(),
                    rack_id=int(row["Rack ID"]),
                    price=row["_price"],
                    latitude=coords[0],
                    longitude=coords[1],
                )
            )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)

        self.stdout.write(
            "\n".join(
                [
                    f"Rows read:             {rows_read}",
                    f"Canadian rows dropped: {canadian}",
                    f"Duplicates collapsed:  {duplicates}",
                    f"Unmatched skipped:     {unmatched}",
                    f"Stations saved:        {len(stations)}",
                ]
            )
        )
