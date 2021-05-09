import os
import numpy as np
from .DataSet import XGSet
from .XGBoost import XGBoost
from dask import dataframe as dd
import dask.distributed
import dask
import yaml
from utils.compute_and_front import  load_all_preprocessed_data

def training(data_dir:str, yml:str, conf:dict, how_batch:str, nw=4, specify_batching='default',printout=1e3):
    """
    :parameter data_dir string path to directory with data
    :parameter out_dir string path to directory to save models
    :parameter how_batch: str, can obtain 2 values, "set_updates", "default"
    :parameter specify_batching
        if  how_batch = "set_updates" then specify_batching is a tuple (n_updates:int, n_batches_pro_update:int)
        if  how_batch = "default" then batches will be formed of size 50mln samples and there will be only 1 update
        resulting in only 2 information print outs
    :parameter printout: int
    :parameter conf dictionary of model parameters
    :parameter nw number of workers for dask
    :parameter yaml string path to yaml file
    specifies how often validation is computed, and results are printed out
    default value is 1e3
    """

    #load data to dask, receive a ddf object
    cluster = dask.distributed.LocalCluster(n_workers=nw, threads_per_worker=1)
    client = dask.distributed.Client(cluster)

    ddf = load_all_preprocessed_data(os.path.abspath(data_dir),True,True)#forlder with preprocessed df
    with open(yml) as yf:
        col_groups = yaml.load(yf,Loader=yaml.CLoader)['available_columns']

    features = col_groups['features']
    classes = ['has_reply','has_retweet','has_retweet_comment','has_like']

    #for every class train own model
    for clazz in classes:
        name = f" Predicting class  {clazz} "
        print(f"{'~' * 10}{name}{'~' * (100-len(name)+8)}")
        #form splitted dataset indicies
        dataset = XGSet(ddf,features,clazz)
        size = dataset.__len__()
        train_ids = (0,int(size*(8/10)))
        valid_ids = (int(size*(8/10)),int(size*(9/10)))
        test_ids = (int(size*(9/10)),size+1)


        if how_batch == 'set_updates':
            n_updates = specify_batching[0] #1e3
            n_batches = specify_batching[1] #pro update
        else:
            batch_size = 50000000
            n_batches = int(size/batch_size)
            n_updates = 1

        model = XGBoost(conf=conf)
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        models_dir = os.path.join(current_dir,"XGB_models",clazz)
        model_in = None
        try:
            os.mkdir(models_dir)
        except:
            pass

        rces = np.zeros((n_updates,2))
        precs = np.zeros((n_updates,2))
        update = 1
        start = 1
        stop = int(train_ids[1]/(n_updates*n_batches))

        # perform fitting into model
        while update <= n_updates:
            #optimised that we train XGboost by giving it data partially, but sequentially
            while stop<=int(train_ids[1]/n_updates)*update:
                X, y = dataset.form_subset((start,stop))
                model_in = model.fit(X,y,model_in,models_dir,update,client)#unique model is save every update
                start = stop
                stop+=int(train_ids[1]/(n_updates*n_batches))


            if not update % printout:
                # validate model
                #validation set is huge, validate it even more partially and parallelised,
                X_val, y_val = dataset.form_subset(valid_ids)
                val_pred = model.predict(X_val,y_val,client,model_in).astype(np.int64)
                val_ap, val_rce = model.evaluate(val_pred,y_val)

                #last part of training subset is efficiently small to fit evaluation
                trn_pred = model.predict(X, y, client,model_in).astype(np.int64)
                trn_ap, trn_rce = model.evaluate(trn_pred,y)

                del X_val, y_val #validation set of 2billion items is huge itself, define it only when validate, and then delete

                rces[update - 1][0], rces[update - 1][1] = val_rce, trn_rce
                precs[update - 1][0], precs[update - 1][1] = val_ap, trn_ap

                print(f"{'='*10} Update {update} {'='*100}")
                print(f"RCE:\n Training   {trn_rce}\n Validation {val_rce}")
                print(f"\nAverage Precision \n Training   {trn_ap}\n Validation {val_ap}")
            update+=1

        #test model
        X_val, y_val = dataset.form_subset(valid_ids)
        X_test, y_test = dataset.form_subset(test_ids)
        val_pred = model.predict(X_val, y_val, client,model_in).astype(np.int64)
        val_ap, val_rce = model.evaluate(val_pred, y_val)
        test_pred = model.predict(X_test, y_test, client,model_in).astype(np.int64)
        test_ap, test_rce = model.evaluate(test_pred, y_test)
        del X_val,X_test,y_test,y_val
        print(f"{'='*10} Final result {'='*96}")
        print(f"RCE:\n Validation {val_rce}\n Test       {test_rce}")
        print(f"\nAverage Precision: \n Validation {val_ap}\n Test       {test_ap}")
