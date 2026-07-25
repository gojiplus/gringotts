"""Minimal API monetized with gringotts.

Run:
    gringotts init-db
    gringotts create-user alice --credits 5
    uvicorn examples.demo_app:app
"""

from fastapi import Depends, FastAPI
from pydantic import BaseModel

import gringotts
from gringotts import CreditedUser, CreditPack, GringottsConfig, charge

app = FastAPI(title="Demo API monetized with gringotts")

gringotts.init_app(
    app,
    GringottsConfig(
        packs=[
            CreditPack(credits=100, price_cents=500, name="Starter"),
            CreditPack(credits=1000, price_cents=4000, name="Pro"),
        ],
    ),
)


class PredictionRequest(BaseModel):
    text: str


@app.post("/predict")
def predict(payload: PredictionRequest, user: CreditedUser = Depends(charge(1))):
    return {"prediction": f"Predicted: {payload.text}", "credits_left": user.credits}
