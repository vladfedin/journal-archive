from ljarchive.desktop import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import sys
        import traceback
        traceback.print_exc()
        sys.exit(1)
