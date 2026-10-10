BGM 曲库目录（storage/bgm）
==========================
本目录存放平台曲库背景音乐文件（mp3/wav/m4a/aac/flac/ogg），由运营方经
POST /api/bgm 上传、DELETE /api/bgm/{name} 下架、GET /api/bgm 列表查询。
成片合成时通过 compose 请求的 bgm.url（/storage/bgm/<文件名>）引用。
用户自定义 BGM 可经 /api/media/upload 上传后以 /storage/uploads/... 地址直传。
