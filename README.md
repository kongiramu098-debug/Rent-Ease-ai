# RentEase AI backend (Flask + MongoDB)

## Run locally
1. Install MongoDB Community, or create a free MongoDB Atlas cluster and copy its URI.
2. `python -m venv venv && source venv/bin/activate`
3. `pip install -r requirements.txt`
4. `cp .env.example .env` and fill in the values
5. `python server.py` (creates indexes, the admin user and 25 laptops on first run), then open http://localhost:5000 for the website

## Local HTTPS
`openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=localhost"`
Set `HTTPS=1` in `.env`, then restart. The server serves https://localhost:5000.
For production use Render (it provides HTTPS) with `gunicorn server:app` and add `seed()` to a one-time script.

## API
POST /api/register, /api/login, /api/logout
GET /api/products?brand=Apple&order=asc (oldest to newest)
POST /api/bookings, GET /api/bookings, POST /api/bookings/<id>/cancel
PATCH /api/bookings/<id>/status (admin)
POST /api/payments/create (mock; replace with Razorpay + signature verification)
GET/POST /api/admin/users, DELETE /api/admin/users/<id>, GET /api/admin/payments

## Known limits
- Availability check then insert can race; use a MongoDB transaction (replica set/Atlas) before going live.
- Payments are simulated. Not tested against a live MongoDB.
