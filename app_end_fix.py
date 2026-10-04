@app.get("/api/node")
def node():  # one account's storage, fetched on its own so a slow account can't hold up the rest
    email = request.args.get("email", "")
    if email not in load():
        return jsonify(error="Unknown account."), 404
    return jsonify(quota(email))


@app.get("/api/status")
def status():
    """Health check and diagnostic endpoint."""
    return jsonify(
        status="ok",
        data_dir=str(DATA),
        client_secret_exists=SECRETS.exists(),
        client_secret_path=str(SECRETS),
        accounts_file_exists=STORE.exists(),
        history_file_exists=HISTORY.exists(),
        redirect_uri=REDIRECT,
        host=HOST,
        port=PORT
    )


if __name__ == "__main__":
    logger.info("Starting Merger application")
    if STORE.exists():
        save(load())  # encrypts a plain-text accounts.json left by an older version
    try:
        from waitress import serve  # sturdier than Flask's development server
        logger.info(f"Starting with Waitress on {HOST}:{PORT}")
        serve(app, host=HOST, port=PORT, threads=8, max_request_body_size=5 * 1024 ** 3)
    except ImportError:
        logger.info(f"Waitress not installed, using Flask dev server on {HOST}:{PORT}")
        app.run(host=HOST, port=PORT, debug=False)
