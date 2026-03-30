import { Router, type IRouter } from "express";
import healthRouter from "./health";
import aliveRouter from "./alive";

const router: IRouter = Router();

router.use(healthRouter);
router.use(aliveRouter);

export default router;
