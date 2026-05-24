from .stealkit import StealKit


async def setup(bot):
    await bot.add_cog(StealKit(bot))
