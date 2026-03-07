# Flipper Zero Compile Server

A real compilation server for Flipper Zero .fap files. Supports Official, Unleashed, RogueMaster and Momentum firmware.

## Deploy to Railway (free)

1. Push this folder to a GitHub repo
2. Go to railway.app and sign in with GitHub
3. Click "New Project" → "Deploy from GitHub repo"
4. Select this repo → Deploy
5. Copy the URL Railway gives you (looks like `https://yourapp.up.railway.app`)

## API

POST /compile
- cFileContent: string
- famFileContent: string  
- cFileName: string
- firmware: "official" | "unleashed" | "roguemaster" | "momentum"

Returns the compiled .fap file as binary.
