from flask import Flask
import os

app = Flask(__name__)

@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
def hello():
    return "Hello World! from test task container."

if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000"))
    app.run(host="0.0.0.0", port=port)
