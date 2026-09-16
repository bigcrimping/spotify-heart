# Spotify Heart

Queues Spotify songs whose tempo matches your heart rate.

A Seeed MR60BHA2 60 GHz mmWave radar (XIAO ESP32-C6) measures your heart rate
without contact. A small Windows tray app reads it over USB, looks up songs
with a matching BPM (or half or double it), and adds one to your Spotify queue
just before the current track ends.

- Heart rate: Seeed MR60BHA2 radar
- Playback: Spotify Web API (Premium account)
- Tempo data: GetSongBPM

## Credits

Tempo data provided by [GetSongBPM](https://getsongbpm.com)
