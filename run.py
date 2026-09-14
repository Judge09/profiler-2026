from app import create_app
import config

app = create_app()

if __name__ == "__main__":
    print(f"\n  PROFILER starting at http://{config.HOST}:{config.PORT}")
    print(f"  Default password: {config.PASSWORD}\n")
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
        threaded=True,
        use_reloader=False,
    )
