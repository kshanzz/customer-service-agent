def main() -> None:
    import uvicorn

    uvicorn.run(
        "api:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
    )


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(override=False)
    main()
