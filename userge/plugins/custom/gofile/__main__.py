import aiohttp

class ProgressFilePayload(aiohttp.payload.Payload):
    def __init__(self, file_path, task_id):
        super().__init__()
        self.file_path = file_path
        self.task_id = task_id

    async def send(self, writer):
        total = os.path.getsize(self.file_path)
        sent = 0
        with open(self.file_path, 'rb') as f:
            async for chunk in iter(lambda: f.read(1024), b''):
                sent += len(chunk)
                speed = len(chunk) / (time.time() - start)
                done = sent
                await update_task(self.task_id, speed=speed, done=done, total=total)
                writer.write(chunk)
                await writer.drain()

async def _upload_to_gofile(file_path, filename):
    task_id = ...  # Obtain task ID
    payload = ProgressFilePayload(file_path, task_id)
    async with aiohttp.ClientSession() as session:
        async with session.post('YOUR_UPLOAD_URL', data=payload) as response:
            ...  # Handle response
