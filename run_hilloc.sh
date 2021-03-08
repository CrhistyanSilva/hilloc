#!/bin/bash
#SBATCH --job-name=hilloc
#SBATCH --ntasks=1
#SBATCH --mem=32768
#SBATCH --time=6:00:00
#SBATCH --tmp=9G
#SBATCH --partition=normal
#SBATCH --qos=gpu
#SBATCH --gres=gpu:1
#SBATCH --mail-type=ALL
#SBATCH --mail-user=cr.silper@gmail.com

source /etc/profile.d/modules.sh
source ~/anaconda/bin/activate
conda activate hilloc
cd ~/proyecto_grado/hilloc/hilloc/experiments
python -m rvae.tf_train --hpconfig depth=1,num_blocks=24,kl_min=0.1,learning_rate=0.002,batch_size=32,enable_iaf=False,dataset=cifar10 --num_gpus 1 --mode train --logdir log

