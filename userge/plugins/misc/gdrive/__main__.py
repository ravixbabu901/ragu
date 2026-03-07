    def _get_file_id(self, filter_str: bool = False) -> tuple:
        link = self._message.input_str
        if filter_str:
            link = self._message.filtered_input_str
        if not link:
            raise ValueError("No Link Provided!")
        # Try to extract a Google Drive file/folder ID from a URL
        found = _GDRIVE_ID.findall(link)
        if found:
            candidate = found[0][0] if isinstance(found[0], tuple) else found[0]
            # Google Drive IDs are always at least 25 characters.
            # If the regex matched something shorter it's a false positive.
            if len(candidate) >= 25:
                return candidate, True
        # If it looks like a Drive URL but regex failed, try query param 'id'
        if link.startswith("https://drive.google.com"):
            from urllib.parse import urlparse, parse_qs  # pylint: disable=import-outside-toplevel
            parsed = urlparse(link)
            file_id = parse_qs(parsed.query).get('id', [None])[0]
            if file_id and len(file_id) >= 25:
                return file_id, True
        # Treat the raw input as a bare file ID
        return link.strip(), False

    # ---- inside Worker class, replace the download method: ----

    @creds_dec
    async def download(self) -> None:
        await self._message.edit("`Loading GDrive Download...`")
        file_id, _ = self._get_file_id()
        pool.submit_thread(self._download, file_id)
        start_t = datetime.now()
        with self._message.cancel_callback(self._cancel):
            while not self._is_finished:
                if self._progress is not None:
                    await self._message.edit(self._progress)
                await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)
        end_t = datetime.now()
        m_s = (end_t - start_t).seconds
        if isinstance(self._output, HttpError):
            out = f"**ERROR** : `{self._output._get_reason()}`"  # pylint: disable=protected-access
        elif self._output is not None and not self._is_canceled:
            # Show only the filename — no leading path like /bot/
            file_name = os.path.basename(str(self._output))
            out = f"**Downloaded Successfully** __in {m_s} seconds__\n\n`{file_name}`"
        elif self._output is not None and self._is_canceled:
            out = self._output
        else:
            out = "`failed to download.. check logs?`"
        await self._message.edit(out, disable_web_page_preview=True, log=__name__)
