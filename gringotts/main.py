from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

# Define request payload model
class PredictionRequest(BaseModel):
    input_string: str
    api_key: str

# Define response payload model
class PredictionResponse(BaseModel):
    output_string: str

# API endpoint for prediction
@app.post("/predict", response_model=PredictionResponse)
async def predict(request: PredictionRequest):
    # Perform authentication and authorization here
    # Check if the API key is valid and has access rights

    # Mock prediction logic
    output_string = f"Predicted: {request.input_string}"

    # Return the prediction result
    return PredictionResponse(output_string=output_string)

# Handle invalid input payload
@app.exception_handler(HTTPException)
async def validation_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"message": exc.detail})

# Handle general exceptions
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    return JSONResponse(status_code=500, content={"message": "Internal server error"})
