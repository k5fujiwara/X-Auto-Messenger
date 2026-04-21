import os
from pathlib import Path

import google.generativeai as genai


def load_env_file():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value

def list_available_models():
    load_env_file()

    # 1. 環境変数からAPIキーを取得
    api_key = os.getenv("GEMINI_API_KEY")
    
    if not api_key:
        print("エラー: 環境変数 'GEMINI_API_KEY' が設定されていません。")
        return

    # 2. Gemini APIの設定
    genai.configure(api_key=api_key)

    print("--- 使用可能なモデル一覧 ---")
    try:
        # 3. 利用可能なモデルをリストアップ
        # supported_generation_methods に 'generateContent' が含まれるものが、
        # 通常のテキスト生成や要約に使用できるモデルです。
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(f"モデル名: {m.name}")
                print(f"  - 表示名: {m.display_name}")
                print(f"  - 説明: {m.description}")
                print("-" * 30)
                
    except Exception as e:
        print(f"エラーが発生しました: {e}")

if __name__ == "__main__":
    list_available_models()