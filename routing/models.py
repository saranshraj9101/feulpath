from django.db import models


class FuelStation(models.Model):
    """A US truck stop with its retail diesel price and city-level coordinates."""

    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.IntegerField()
    # Full source precision (e.g. 3.007333); round only for display.
    price = models.DecimalField(max_digits=9, decimal_places=6)
    latitude = models.FloatField()
    longitude = models.FloatField()

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
