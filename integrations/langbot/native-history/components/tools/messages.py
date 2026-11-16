from langbot_plugin.api.definition.components.tool.tool import Tool


class NativeGroupMessages(Tool):
    async def call(self, params, session, query_id):
        return await self.plugin.invoke("messages", params, session, query_id)
