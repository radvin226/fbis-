"""جست‌وجوی سریع موزیک با yt-dlp و Google fallback."""
import asyncio
import os

_CACHE={}

def _google_search_sync(query, limit=3):
    key="g:"+query
    if key in _CACHE: return _CACHE[key]
    try:
        from googlesearch import search as gsearch
        out=[]
        for url in gsearch(query + " audio", num_results=limit):
            out.append({"title":query,"url":url})
        _CACHE[key]=out
        return out
    except Exception:
        return []

def _search_sync(query, limit=5):
    key="s:"+query
    if key in _CACHE: return _CACHE[key]
    try:
        import yt_dlp
        opts={"quiet":True,"no_warnings":True,"skip_download":True,"extract_flat":True,"noplaylist":True,"socket_timeout":5}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info=ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        out=[]
        for item in info.get("entries") or []:
            if item:
                url=item.get("webpage_url") or item.get("url")
                if url: out.append({"title":item.get("title") or query,"url":url})
        if out:
            _CACHE[key]=out
            return out
    except Exception:
        pass
    return _google_search_sync(query, limit)

async def search(query, limit=5):
    return await asyncio.to_thread(_search_sync, query, limit)

def _download_sync(url,out_dir):
    import yt_dlp
    os.makedirs(out_dir,exist_ok=True)
    template=os.path.join(out_dir,"%(title).80s [%(id)s].%(ext)s")
    opts={
      "format":"bestaudio[ext=m4a]/bestaudio/best",
      "outtmpl":template,
      "noplaylist":True,
      "quiet":True,
      "no_warnings":True,
      "restrictfilenames":True,
      "concurrent_fragment_downloads":4,
      "postprocessors":[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"128"}]
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info=ydl.extract_info(url,download=True)
        path=ydl.prepare_filename(info)
    mp3=os.path.splitext(path)[0]+".mp3"
    return mp3 if os.path.isfile(mp3) else path

async def download_audio(url,out_dir):
    return await asyncio.to_thread(_download_sync,url,out_dir)
