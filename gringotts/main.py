from fastapi import FastAPI, Request
from pydantic import BaseModel

from .decorators import requires_credits
from . import create_tables

# Ensure tables exist
create_tables.create_tables()

app = FastAPI()


class PredictionRequest(BaseModel):
    input_string: str


class PredictionResponse(BaseModel):
    output_string: str


@app.post("/predict", response_model=PredictionResponse)
@requires_credits(cost=1)
async def predict(request: Request, payload: PredictionRequest):
    output_string = f"Predicted: {payload.input_string}"
    return PredictionResponse(output_string=output_string)
