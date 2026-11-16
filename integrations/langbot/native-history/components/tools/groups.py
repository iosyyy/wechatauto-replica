from langbot_plugin.api.definition.components.tool.tool import Tool


class NativeGroups(Tool):
    async def call(self, params, session, query_id):
        return await self.plugin.invoke("groups", params, session, query_id)
