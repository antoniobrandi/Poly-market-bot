import { Router, type IRouter } from "express";

const router: IRouter = Router();

router.get("/alive", (_req, res) => {
  res.json({ status: "alive" });
});

export default router;
