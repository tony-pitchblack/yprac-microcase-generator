# PyTaskSyn Backend

FastAPI backend service for generating microcases.

## Setup

1. Use the global micromamba environment:
```bash
micromamba activate ymg
```

2. The ngrok auth token is already configured in `.env` file as `NGROK_AUTHTOKEN`

## Running

Both scripts are executable and load `.env` from the root folder automatically.

### From anywhere in the project:
```bash
# Local development
python pytasksyn-backend/main.py
# or
./pytasksyn-backend/main.py

# With ngrok tunnel
python pytasksyn-backend/run_with_ngrok.py
# or
./pytasksyn-backend/run_with_ngrok.py
```

### From backend directory:
```bash
cd pytasksyn-backend

# Local development
python main.py
# or
./main.py

# With ngrok tunnel
python run_with_ngrok.py
# or
./run_with_ngrok.py
```

## API Endpoints

### POST /gen-microcases/
Accepts JSON payload with:
- `url`: string
- `user_id`: string

Prints the received data to terminal and returns a confirmation response.